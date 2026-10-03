# restoreVolumeGroups = {'fs9500-2.cpolab.ibm.com' : {'kvm1-rmdb-config1-restore' : 'ORION15-mdb7', 'kvm1-rmdb1-restore' : 'ORION16-mdb8', 'kvm1-rmdb2-restore' : 'ORION17-mdb9'},
#                        'fs9500-3.cpolab.ibm.com' : {'kvm2-rmdb-config2-restore' : 'ORION15-mdb7-restore', 'kvm2-rmdb3-restore' : 'ORION16-mdb8-restore', 'kvm2-rmdb4-restore' : 'ORION17-mdb9-restore',
#                                                     'kvm3-rmdb7-restore' : 'ORION15-mdb7-restore', 'kvm3-rmdb8-restore' : 'ORION16-mdb8-restore', 'kvm3-rmdb9-restore' : 'ORION17-mdb9-restore'}}
# System Name { target VG : source VG }

restoreVolumeGroups = {'fs9500-2.cpolab.ibm.com' : {'kvm1-rmdb-config1-restore' : 'ORION15-mdb7', 'kvm1-rmdb1-restore' : 'ORION16-mdb8', 'kvm1-rmdb2-restore' : 'ORION17-mdb9'},
                       'fs9500-3.cpolab.ibm.com' : {'kvm2-rmdb-config2-restore' : 'ORION15-mdb7-restore', 'kvm2-rmdb3-restore' : 'ORION16-mdb8-restore', 'kvm2-rmdb4-restore' : 'ORION17-mdb9-restore',
                                                    'kvm3-rmdb7-restore' : 'ORION15-mdb7-restore', 'kvm3-rmdb8-restore' : 'ORION16-mdb8-restore', 'kvm3-rmdb9-restore' : 'ORION17-mdb9-restore' }}



#volumeGroups = { 'fs9500-2.cpolab.ibm.com' : ['ORION15-mdb7', 'ORION16-mdb8', 'ORION17-mdb9']}
#volumeGroupsReplication = {'fs9500-2.cpolab.ibm.com' : {'ORION15-mdb7': 'ORION15-mdb7-restore', 'ORION16-mdb8' : 'ORION16-mdb8-restore', 'ORION17-mdb9': 'ORION17-mdb9-restore'}}
#remoteVolumeGroupSnaphots = {'fs9500-3.cpolab.ibm.com' : {'ORION15-mdb7-restore' : 'ORION15-mdb7', 'ORION16-mdb8-restore' : 'ORION16-mdb8', 'ORION17-mdb9-restore' : 'ORION17-mdb9'}}