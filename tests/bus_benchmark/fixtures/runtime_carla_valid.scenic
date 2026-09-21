param map = localPath('../../../safebench/scenario/scenario_data/scenic_data/maps/Town05.xodr')
param carla_map = 'Town05'
param address = '127.0.0.1'
param port = 2000
model scenic.simulators.carla.model

EGO_MODEL = 'vehicle.chevrolet.impala'

ego = Car at -188@37,
    with regionContainedIn None,
    with blueprint EGO_MODEL,
    with length 5.33,
    with width 2.10
