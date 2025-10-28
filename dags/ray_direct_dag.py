from airflow.decorators import dag, task
from datetime import datetime

@dag(start_date=datetime(2025,1,1), schedule=None, catchup=False, tags=["ray"])
def ray_direct_dag():
    @task
    def run_on_ray(nums: list):
        import ray
        # connect to an existing Ray cluster. If you want a local single-node Ray use ray.init()
        ray.init(address="auto", ignore_reinit_error=True)
        @ray.remote
        def square(x):
            return x * x
        futures = [square.remote(n) for n in nums]
        results = ray.get(futures)
        ray.shutdown()
        return results

    run_on_ray([1,2,3,4])

dag = ray_direct_dag()
