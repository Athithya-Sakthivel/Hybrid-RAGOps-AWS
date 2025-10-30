.PHONY: s3 delete-s3 tree clean lc push 

push:
	git add .
	git commit -m "new"
	git push origin main --force

s3:
	python3 utils/s3_bucket.py --create
	aws s3 ls "s3://$S3_BUCKET/" --recursive | head -n 100

delete-s3:
	python3 utils/s3_bucket.py --create
	aws s3 ls
	
tree:
	tree -a -I '.git|.venv|repos|__pycache__|venv|commands.sh|production-stack|raw_data|.venv2|archive|tmp.md|docs|models|tmp|raw|chunked'

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + && find . -name "*.pyc" -delete
	clear

index:
	bash indexing_pipeline/run.sh

push-repo-to-ray:
	ray rsync-up cluster.yaml ./ /workspace --all-nodes

pull-repo-from-ray:
	ray rsync-down cluster.yaml ./ /workspace --all-nodes
	ray start --restart-only

monitor-models:
	ray status
	serve status