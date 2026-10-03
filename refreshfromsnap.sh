SNAPID=$1

echo "Restore snaps ${SNAPID} on FS9500-2"
ssh FS9500-2 "svctask refreshfromsnapshot -fromsourcegroup ORION16-mdb8 -gui -snapshot ORION16-mdb8_${SNAPID} -volumegroup ORION0A-kvm1-mdb1-restore"
ssh FS9500-2 "svctask refreshfromsnapshot -fromsourcegroup ORION16-mdb8 -gui -snapshot ORION16-mdb8_${SNAPID} -volumegroup ORION16-mdb8-restore"
ssh FS9500-2 "svctask refreshfromsnapshot -fromsourcegroup ORION17-mdb9 -gui -snapshot ORION17-mdb9_${SNAPID} -volumegroup ORION17-mdb9-restore"
ssh FS9500-2 "svctask refreshfromsnapshot -fromsourcegroup ORION17-mdb9 -gui -snapshot ORION17-mdb9_${SNAPID} -volumegroup ORION0A-kvm1-mdb2-restore"
ssh FS9500-2 "svctask refreshfromsnapshot -fromsourcegroup ORION15-mdb7 -gui -snapshot ORION15-mdb7_${SNAPID} -volumegroup ORION0A-kvm1-mdb-config1-restore"
ssh FS9500-2 "svctask refreshfromsnapshot -fromsourcegroup ORION15-mdb7 -gui -snapshot ORION15-mdb7_${SNAPID} -volumegroup ORION15-mdb7-restore"

echo "Restore snaps ${SNAPID} on FS9500-3"
ssh FS9500-3 "svctask refreshfromsnapshot -fromsourcegroup RecoveryShards -gui -snapshot RecoveryShards_${SNAPID} -volumegroup ORION0B-kvm2-mdb3-restore"
ssh FS9500-3 "svctask refreshfromsnapshot -fromsourcegroup RecoveryShards -gui -snapshot RecoveryShards_${SNAPID} -volumegroup ORION0B-kvm2-mdb4-restore"
ssh FS9500-3 "svctask refreshfromsnapshot -fromsourcegroup RecoveryShards -gui -snapshot RecoveryShards_${SNAPID} -volumegroup ORION0B-kvm2-mdb-config2-restore"

echo "Done restoring snaps."




