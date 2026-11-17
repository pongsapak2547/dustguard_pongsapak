#!/usr/bin/env bash
# =====================================================================
# DustGuard: สร้าง/อัปเดตทรัพยากร AWS ทั้งหมด (รันซ้ำได้ ไม่พัง)
# วิธีใช้ (ใน AWS CloudShell, region us-east-1):
#   cd dustguard
#   bash scripts/deploy.sh
# =====================================================================
set -euo pipefail

export AWS_DEFAULT_REGION=us-east-1
export AWS_PAGER=""
cd "$(dirname "$0")/.."

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/LabRole"
SUFFIX="${SUFFIX:-$ACCOUNT_ID}"          # ใช้เลข account เป็น suffix จะได้ไม่ซ้ำใครในโลก
BUCKET="dustguard-data-${SUFFIX}"
T_READ="dustguard-readings"
T_SUB="dustguard-subscribers"
TOPIC_NAME="dustguard-alerts"
FN_CHECKER="dustguard-checker"
FN_API="dustguard-api"
RULE="dustguard-hourly"
API_NAME="dustguard-http-api"
APP_NAME="dustguard-web"
BUILD=$(mktemp -d)

step() { echo; echo "==== $* ===="; }

# ---------------------------------------------------------------------
step "1/8 DynamoDB: $T_READ, $T_SUB"
if ! aws dynamodb describe-table --table-name "$T_READ" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$T_READ" \
    --attribute-definitions AttributeName=province,AttributeType=S AttributeName=ts,AttributeType=S \
    --key-schema AttributeName=province,KeyType=HASH AttributeName=ts,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST >/dev/null
  echo "สร้าง $T_READ แล้ว"
else echo "$T_READ มีอยู่แล้ว"; fi

if ! aws dynamodb describe-table --table-name "$T_SUB" >/dev/null 2>&1; then
  aws dynamodb create-table --table-name "$T_SUB" \
    --attribute-definitions AttributeName=email,AttributeType=S \
    --key-schema AttributeName=email,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  echo "สร้าง $T_SUB แล้ว"
else echo "$T_SUB มีอยู่แล้ว"; fi
aws dynamodb wait table-exists --table-name "$T_READ"
aws dynamodb wait table-exists --table-name "$T_SUB"

# ---------------------------------------------------------------------
step "2/8 S3: $BUCKET + config.json"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  echo "สร้าง bucket แล้ว"
else echo "bucket มีอยู่แล้ว"; fi
aws s3 cp config/config.json "s3://$BUCKET/config.json" --content-type "application/json; charset=utf-8"

# ---------------------------------------------------------------------
step "3/8 SNS topic: $TOPIC_NAME (Standard)"
TOPIC_ARN=$(aws sns create-topic --name "$TOPIC_NAME" --query TopicArn --output text)
echo "$TOPIC_ARN"

# ---------------------------------------------------------------------
# สร้างหรืออัปเดต Lambda: deploy_lambda <ชื่อ> <โฟลเดอร์> <env-json>
deploy_lambda() {
  local name=$1 dir=$2 env=$3 zipfile="$BUILD/$1.zip"
  (cd "$dir" && zip -q -j "$zipfile" lambda_function.py)
  if aws lambda get-function --function-name "$name" >/dev/null 2>&1; then
    aws lambda update-function-code --function-name "$name" --zip-file "fileb://$zipfile" >/dev/null
    aws lambda wait function-updated --function-name "$name"
    aws lambda update-function-configuration --function-name "$name" \
      --timeout 60 --memory-size 256 --environment "$env" >/dev/null
    echo "อัปเดต $name แล้ว"
  else
    aws lambda create-function --function-name "$name" \
      --runtime python3.12 --handler lambda_function.lambda_handler \
      --role "$ROLE_ARN" --zip-file "fileb://$zipfile" \
      --timeout 60 --memory-size 256 --environment "$env" >/dev/null
    echo "สร้าง $name แล้ว"
  fi
  aws lambda wait function-updated --function-name "$name"
}

# เพิ่มสิทธิ์ให้บริการอื่นเรียก Lambda (ถ้ามีแล้วข้าม)
add_permission() {
  aws lambda add-permission --function-name "$1" --statement-id "$2" \
    --action lambda:InvokeFunction --principal "$3" --source-arn "$4" >/dev/null 2>&1 \
    && echo "เพิ่มสิทธิ์ $2 แล้ว" || echo "สิทธิ์ $2 มีอยู่แล้ว"
}

step "4/8 Lambda: $FN_CHECKER"
deploy_lambda "$FN_CHECKER" lambdas/checker \
  "{\"Variables\":{\"TABLE_READINGS\":\"$T_READ\",\"BUCKET\":\"$BUCKET\",\"TOPIC_ARN\":\"$TOPIC_ARN\"}}"
CHECKER_ARN=$(aws lambda get-function --function-name "$FN_CHECKER" --query Configuration.FunctionArn --output text)

# ---------------------------------------------------------------------
step "5/8 EventBridge: $RULE (ทุก 1 ชั่วโมง)"
RULE_ARN=$(aws events put-rule --name "$RULE" --schedule-expression "rate(1 hour)" --state ENABLED --query RuleArn --output text)
add_permission "$FN_CHECKER" eventbridge-hourly events.amazonaws.com "$RULE_ARN"
aws events put-targets --rule "$RULE" --targets "Id=checker,Arn=$CHECKER_ARN" >/dev/null
echo "$RULE -> $FN_CHECKER"

# ---------------------------------------------------------------------
step "6/8 Lambda: $FN_API"
deploy_lambda "$FN_API" lambdas/api \
  "{\"Variables\":{\"TABLE_SUBSCRIBERS\":\"$T_SUB\",\"TABLE_READINGS\":\"$T_READ\",\"BUCKET\":\"$BUCKET\",\"TOPIC_ARN\":\"$TOPIC_ARN\"}}"
API_FN_ARN=$(aws lambda get-function --function-name "$FN_API" --query Configuration.FunctionArn --output text)

# ---------------------------------------------------------------------
step "7/8 API Gateway (HTTP API): $API_NAME"
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text)
CORS='{"AllowOrigins":["*"],"AllowMethods":["GET","POST","OPTIONS"],"AllowHeaders":["content-type"]}'
if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  API_ID=$(aws apigatewayv2 create-api --name "$API_NAME" --protocol-type HTTP \
    --cors-configuration "$CORS" --query ApiId --output text)
  echo "สร้าง API แล้ว: $API_ID"
else
  aws apigatewayv2 update-api --api-id "$API_ID" --cors-configuration "$CORS" >/dev/null
  echo "API มีอยู่แล้ว: $API_ID"
fi

INTEGRATION_ID=$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
  --query "Items[?IntegrationUri=='$API_FN_ARN'].IntegrationId | [0]" --output text)
if [ "$INTEGRATION_ID" = "None" ] || [ -z "$INTEGRATION_ID" ]; then
  INTEGRATION_ID=$(aws apigatewayv2 create-integration --api-id "$API_ID" \
    --integration-type AWS_PROXY --integration-uri "$API_FN_ARN" \
    --payload-format-version 2.0 --query IntegrationId --output text)
fi

for ROUTE in "GET /provinces" "GET /readings" "POST /subscribe"; do
  EXISTS=$(aws apigatewayv2 get-routes --api-id "$API_ID" --query "Items[?RouteKey=='$ROUTE'].RouteId | [0]" --output text)
  if [ "$EXISTS" = "None" ] || [ -z "$EXISTS" ]; then
    aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$ROUTE" --target "integrations/$INTEGRATION_ID" >/dev/null
    echo "สร้าง route $ROUTE"
  fi
done

if ! aws apigatewayv2 get-stage --api-id "$API_ID" --stage-name '$default' >/dev/null 2>&1; then
  aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy >/dev/null
fi
add_permission "$FN_API" apigateway-invoke apigateway.amazonaws.com \
  "arn:aws:execute-api:us-east-1:${ACCOUNT_ID}:${API_ID}/*/*"

API_URL="https://${API_ID}.execute-api.us-east-1.amazonaws.com"
echo "const API_URL = '${API_URL}';" > web/config.js
echo "API_URL = $API_URL (เขียนลง web/config.js แล้ว)"

# ---------------------------------------------------------------------
step "8/8 Amplify: $APP_NAME (Deploy without Git)"
APP_ID=$(aws amplify list-apps --query "apps[?name=='$APP_NAME'].appId | [0]" --output text)
if [ "$APP_ID" = "None" ] || [ -z "$APP_ID" ]; then
  APP_ID=$(aws amplify create-app --name "$APP_NAME" --query app.appId --output text)
  aws amplify create-branch --app-id "$APP_ID" --branch-name main >/dev/null
  echo "สร้าง Amplify app แล้ว: $APP_ID"
fi
(cd web && zip -q -r "$BUILD/web.zip" .)   # zip ไฟล์ข้างใน web/ ไม่ใช่ตัวโฟลเดอร์
DEPLOY=$(aws amplify create-deployment --app-id "$APP_ID" --branch-name main --output json)
JOB_ID=$(echo "$DEPLOY" | python3 -c 'import sys,json;print(json.load(sys.stdin)["jobId"])')
UPLOAD_URL=$(echo "$DEPLOY" | python3 -c 'import sys,json;print(json.load(sys.stdin)["zipUploadUrl"])')
curl -s -X PUT -H "Content-Type: application/zip" --data-binary "@$BUILD/web.zip" "$UPLOAD_URL" >/dev/null
aws amplify start-deployment --app-id "$APP_ID" --branch-name main --job-id "$JOB_ID" >/dev/null
echo "กำลัง deploy เว็บ (รอ ~30 วินาที)"

rm -rf "$BUILD"
cat <<EOF

=====================================================================
 เสร็จแล้ว
 เว็บ:        https://main.${APP_ID}.amplifyapp.com
 API:         ${API_URL}
 S3 bucket:   ${BUCKET}
 SNS topic:   ${TOPIC_ARN}

 ขั้นต่อไป: เปิดเว็บ -> สมัครด้วยอีเมล -> กด Confirm ในอีเมล
            -> bash scripts/test_alert.sh
=====================================================================
EOF
