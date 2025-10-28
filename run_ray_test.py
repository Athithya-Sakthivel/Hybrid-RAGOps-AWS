import ray
if __name__ == "__main__":
    # connect to an existing Ray cluster; if none exists this will start a local one
    ray.init(address="auto", ignore_reinit_error=True)
    @ray.remote
    def inc(x): return x + 1
    print(ray.get([inc.remote(i) for i in range(5)]))
    ray.shutdown()
