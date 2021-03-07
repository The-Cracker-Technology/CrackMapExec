rm -rf /opt/ANDRAX/CrackMapExec
cp -Rf andraxbin/* /opt/ANDRAX/bin

cp -Rf config /opt/ANDRAX/.config

rm -rf /opt/ANDRAX/cachevirtual/virtualenvs/*crackmapexec*

sudo cp -Rf /opt/ANDRAX/.config/ /root/.config/
sudo cp -Rf /opt/ANDRAX/.config/ /home/andrax/.config/

sudo chmod -R 777 /root/.config /home/andrax/.config/

sleep 01

cp -Rf $(pwd) /opt/ANDRAX/CrackMapExec

cd /opt/ANDRAX/CrackMapExec

poetry install
