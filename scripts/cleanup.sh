#!/usr/bin/env bash
# ลบทรัพยากร DustGuard ทั้งหมด (ใช้หลัง demo) ลบแล้วกู้คืนไม่ได้
set -uo pipefail
export AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET="dustguard-data-${SUFFIX:-$ACCOUNT_ID}"

read -r -p "จะลบทรัพยากร DustGuard ทั้งหมด (รวม bucket $BUCKET) พิมพ์ yes เพื่อยืนยัน: " OK
[ "$OK" = "yes" ] || { echo "ยกเลิก"; exit 1; }

echo "- EventBridge rule"
aws events remove-targets --rule dustguard-hourly --ids checker 2>/dev/null
aws events delete-rule --name dustguard-hourly 2>/dev/null

echo "- Lambda"
aws lambda delete-function --function-name dustguard-checker 2>/dev/null
aws lambda delete-function --function-name dustguard-api 2>/dev/null

echo "- API Gateway"
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='dustguard-http-api'].ApiId | [0]" --output text)
[ "$API_ID" != "None" ] && aws apigatewayv2 delete-api --api-id "$API_ID"

echo "- Amplify"
APP_ID=$(aws amplify list-apps --query "apps[?name=='dustguard-web'].appId | [0]" --output text)
[ "$APP_ID" != "None" ] && aws amplify delete-app --app-id "$APP_ID" >/dev/null

echo "- SNS (subscription หายตาม)"
aws sns delete-topic --topic-arn "arn:aws:sns:us-east-1:${ACCOUNT_ID}:dustguard-alerts" 2>/dev/null

echo "- DynamoDB"
aws dynamodb delete-table --table-name dustguard-readings >/dev/null 2>&1
aws dynamodb delete-table --table-name dustguard-subscribers >/dev/null 2>&1

echo "- S3 (Empty แล้ว Delete)"
aws s3 rm "s3://$BUCKET" --recursive >/dev/null 2>&1
aws s3api delete-bucket --bucket "$BUCKET" 2>/dev/null

echo "เสร็จ อย่าลืมกด End Lab"
