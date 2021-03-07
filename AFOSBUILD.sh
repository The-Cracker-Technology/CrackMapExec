rm -rf /opt/ANDRAX/CrackMapExec
cp -Rf andraxbin/* /opt/ANDRAX/bin

cp -Rf .config /opt/ANDRAX/.config

rm -rf /opt/ANDRAX/cachevirtual/virtualenvs/*crackmapexec*

sudo cp -Rf /opt/ANDRAX/.config/pypoetry /root/.config/
sudo cp -Rf /opt/ANDRAX/.config/pypoetry /home/andrax/.config/

cp -Rf $(pwd) /opt/ANDRAX/CrackMapExec

cd /opt/ANDRAX/CrackMapExec

poetry install
