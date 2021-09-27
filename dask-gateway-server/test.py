from dask_gateway import Gateway, BasicAuth
auth = BasicAuth(username=None, password="asdf")
gateway = Gateway(address="http://172.16.8.64:8000", auth=auth)
cluster = gateway.new_cluster()

cluster.scale(1)

client = cluster.get_client()

def square(x):
    return x ** 2

def neg(x):
    return -x


A = client.map(square, range(10))
B = client.map(neg, A)
total = client.submit(sum, B)
print(total.result())
import time
while True:
    time.sleep(5)

