ray stop --force || true && sudo rm -rf /tmp/ray/ && ray start --head && python3 infra/rayserve_models_cpu.py

