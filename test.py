from dask_gateway import Gateway, BasicAuth
auth = BasicAuth(username=None, password="asdf")
gateway = Gateway(address="http://172.16.8.64:8000", auth=auth)
cluster = gateway.new_cluster()
cluster.scale(1)
