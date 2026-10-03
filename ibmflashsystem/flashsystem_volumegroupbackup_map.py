## for TestRep
#volumeGroups = { 'fs9500-2.cpolab.ibm.com' : ['ORION16-FiServRep-mdb7', 'ORION16-FiServRep-mdb8', 'ORION16-FiServRep-mdb9']}
#volumeGroupsReplication = {'fs9500-2.cpolab.ibm.com' : {'ORION16-FiServRep-mdb7': 'ORION16-FiServRep-mdb7-restore', 'ORION16-FiServRep-mdb8' : 'ORION16-FiServRep-mdb8-restore', 'ORION16-FiServRep-mdb9': 'ORION16-FiServRep-mdb9-restore'}}
#remoteVolumeGroupSnaphots = {'fs9500-3.cpolab.ibm.com' : {'ORION16-FiServRep-mdb7-restore' : 'ORION16-FiServRep-mdb7', 'ORION16-FiServRep-mdb8-restore' : 'ORION16-FiServRep-mdb8', 'ORION16-FiServRep-mdb9-restore' : 'ORION16-FiServRep-mdb9'}}



## For sgcCluster
volumeGroups = { 'fs9500-2.cpolab.ibm.com' : ['ORION15-mdb7', 'ORION16-mdb8', 'ORION17-mdb9']}
volumeGroupsReplication = {'fs9500-2.cpolab.ibm.com' : {'ORION15-mdb7': 'ORION15-mdb7-restore', 'ORION16-mdb8' : 'ORION16-mdb8-restore', 'ORION17-mdb9': 'ORION17-mdb9-restore'}}
remoteVolumeGroupSnaphots = {'fs9500-3.cpolab.ibm.com' : {'ORION15-mdb7-restore' : 'ORION15-mdb7', 'ORION16-mdb8-restore' : 'ORION16-mdb8', 'ORION17-mdb9-restore' : 'ORION17-mdb9'}}



# volumeGroups = { 'fs9500-2.cpolab.ibm.com' : ['ORION15-mdb7', 'ORION17-mdb9', 'ORION16-mdb8', 'ORION0A-kvm1-mdb-config1', 'ORION0A-kvm1-mdb1', 'ORION0A-kvm1-mdb2'],
#                  'fs9500-3.cpolab.ibm.com' : ['ORION0B-kvm2-mdb-config2', 'ORION0B-kvm2-mdb3', 'ORION0B-kvm2-mdb4'] }
# volumeGroups = { 'fs9500-2.cpolab.ibm.com' : ['ORION18-mdb10'] }

