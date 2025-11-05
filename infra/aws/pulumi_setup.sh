#!/usr/bin/env bash
set -euo pipefail
AWS_REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:-pulumi-state-yourteam-prod}"
DDB_TABLE="${DDB_TABLE:-pulumi-state-locks}"
PULUMI_IAM_USER="${PULUMI_IAM_USER:-pulumi-ci-user}"

aws configure set region "$AWS_REGION"

aws s3api create-bucket --bucket "$S3_BUCKET" --region "$AWS_REGION" $( [ "$AWS_REGION" = "us-east-1" ] && echo "" || echo "--create-bucket-configuration LocationConstraint=$AWS_REGION" )
aws s3api put-bucket-versioning --bucket "$S3_BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$S3_BUCKET" --server-side-encryption-configuration '{
  "Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]
}'
aws dynamodb create-table --table-name "$DDB_TABLE" --attribute-definitions AttributeName=LockID,AttributeType=S --key-schema AttributeName=LockID,KeyType=HASH --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 --region "$AWS_REGION"
aws dynamodb wait table-exists --table-name "$DDB_TABLE" --region "$AWS_REGION"

cat > /tmp/pulumi-state-policy.json <<'POL'
{
  "Version":"2012-10-17",
  "Statement":[
    {
      "Effect":"Allow",
      "Action":[
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket",
        "s3:GetBucketVersioning",
        "s3:PutBucketVersioning"
      ],
      "Resource":[
        "arn:aws:s3:::'"$S3_BUCKET"'",
        "arn:aws:s3:::'"$S3_BUCKET"'/*"
      ]
    },
    {
      "Effect":"Allow",
      "Action":[
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:DeleteItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:ConditionCheckItem"
      ],
      "Resource":[
        "arn:aws:dynamodb:'"$AWS_REGION"':"$(aws sts get-caller-identity --query Account --output text)":table/'"$DDB_TABLE"'"
      ]
    }
  ]
}
POL

aws iam create-policy --policy-name PulumiStateAccessPolicy --policy-document file:///tmp/pulumi-state-policy.json || true
POLICY_ARN=$(aws iam list-policies --query "Policies[?PolicyName=='PulumiStateAccessPolicy'].Arn" --output text)

aws iam create-user --user-name "$PULUMI_IAM_USER" || true
aws iam attach-user-policy --user-name "$PULUMI_IAM_USER" --policy-arn "$POLICY_ARN"
CREDS_FILE="/tmp/pulumi-ci-credentials.json"
aws iam create-access-key --user-name "$PULUMI_IAM_USER" > "$CREDS_FILE"

echo "Pulumi backend bucket: $S3_BUCKET"
echo "DynamoDB lock table: $DDB_TABLE"
echo "Policy ARN: $POLICY_ARN"
echo "Credentials saved to $CREDS_FILE (store securely and remove file when done)."
