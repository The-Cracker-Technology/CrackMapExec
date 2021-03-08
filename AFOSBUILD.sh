rm -rf /opt/ANDRAX/CrackMapExec
cp -Rf andraxbin/* /opt/ANDRAX/bin

chmod -R 755 /opt/ANDRAX/bin

cp -Rf config /opt/ANDRAX/.config

rm -rf /opt/ANDRAX/cachevirtual/virtualenvs/*crackmapexec*

sudo cp -Rf /opt/ANDRAX/.config /root/
sudo cp -Rf /opt/ANDRAX/.config /home/andrax/

sudo chmod -R 777 /root/.config /home/andrax/

sleep 01

cp -Rf $(pwd) /opt/ANDRAX/CrackMapExec

cd /opt/ANDRAX/CrackMapExec

poetry install
