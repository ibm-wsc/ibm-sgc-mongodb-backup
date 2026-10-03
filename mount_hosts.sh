baremetal=()
kvms=(rmdb1 rmdb2 rmdb3 rmdb4 rmdb-config1 rmdb-config2 rmdb7 rmdb8 rmdb9)

for xhost in ${kvms[@]};
do
	echo "Mounting mongodb on ${xhost}."
	result=$(ssh $xhost 'mount /fimongo1 2>&1');
	result=$(ssh $xhost 'chown -R mongod:mongod /fimongo1 2>&1');

        sleep 1
done
for xhost in ${baremetal[@]};
do
	echo "Mounting mongodb on ${xhost}."
        result=$(ssh $xhost 'mount /fimongo1 2>&1');
        sleep 1
done


