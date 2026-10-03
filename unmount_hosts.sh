mdbhosts=(rmdb1 rmdb2 rmdb3 rmdb4 rmdb-config1 rmdb-config2 rmdb7 rmdb8 rmdb9)

allclear=1
while [ $allclear -ne 0 ];
do
        allclear=0
        for xhost in ${mdbhosts[@]};
        do
                # echo -n $xhost;
                result=$(ssh $xhost 'umount /fimongo1 2>&1');
                #echo $result
                if [[ "$result" == *"busy"* ]];
                then
                        echo "Host: ${xhost} is currently busy."
                        allclear+=1
                fi
        done
        echo " Busy hosts: ${allclear}, sleeping.";
        sleep 10
done

kvmhosts=(mdb-kvm1 mdb-kvm2 mdb-kvm3)
for xhost in ${kvmhosts[@]};
do
        echo "Resolving KVM Cache on Host: ${xhost}"
        ssh $xhost 'sync && echo 3 > /proc/sys/vm/drop_caches' &
done
