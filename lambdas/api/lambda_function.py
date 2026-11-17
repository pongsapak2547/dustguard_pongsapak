"""
dustguard-api  (API Gateway HTTP API, payload v2.0)
  GET  /provinces  -> รายชื่อจังหวัดจาก config.json
  GET  /readings   -> ค่าฝุ่นล่าสุดของทุกจังหวัด
  POST /subscribe  -> {"email": "...", "province": "..."} สมัครรับอีเมลเตือน
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

TH_TZ = timezone(timedelta(hours=7))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

TABLE_SUBSCRIBERS = os.environ.get("TABLE_SUBSCRIBERS", "dustguard-subscribers")
TABLE_READINGS = os.environ.get("TABLE_READINGS", "dustguard-readings")
BUCKET = os.environ.get("BUCKET", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")

s3 = boto3.client("s3")
sns = boto3.client("sns")
dynamodb = boto3.resource("dynamodb")
subscribers_table = dynamodb.Table(TABLE_SUBSCRIBERS)
readings_table = dynamodb.Table(TABLE_READINGS)


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    raise TypeError


def respond(status, body):
    # CORS หลักตั้งที่ API Gateway แต่ใส่ไว้ตรงนี้ด้วยเผื่อทดสอบผ่าน Function URL
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, ensure_ascii=False, default=_json_default),
    }


def load_config():
    obj = s3.get_object(Bucket=BUCKET, Key="config.json")
    return json.loads(obj["Body"].read().decode("utf-8"))


def get_provinces():
    config = load_config()
    return respond(200, {"provinces": config["provinces"], "threshold": config.get("threshold", 37.5)})


def get_readings():
    config = load_config()
    readings = []
    for province in config["provinces"]:
        resp = readings_table.query(
            KeyConditionExpression=Key("province").eq(province),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        if items:
            it = items[0]
            readings.append({"province": province, "pm25": it["pm25"], "ts": it["ts"], "stations": it.get("stations")})
        else:
            readings.append({"province": province, "pm25": None, "ts": None, "stations": 0})
    return respond(200, {"threshold": config.get("threshold", 37.5), "readings": readings})


def subscribe(event):
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64
            body = base64.b64decode(body).decode("utf-8")
        data = json.loads(body)
    except (ValueError, TypeError):
        return respond(400, {"error": "รูปแบบข้อมูลไม่ถูกต้อง"})

    email = str(data.get("email", "")).strip().lower()
    province = str(data.get("province", "")).strip()

    if not EMAIL_RE.match(email) or len(email) > 254:
        return respond(400, {"error": "รูปแบบอีเมลไม่ถูกต้อง"})
    if province not in load_config()["provinces"]:
        return respond(400, {"error": "ไม่มีจังหวัดนี้ในระบบ"})

    filter_policy = json.dumps({"province": [province]}, ensure_ascii=False)

    # ถ้าเคยสมัครแล้ว และ subscription ยืนยันแล้ว -> แค่เปลี่ยนจังหวัด (แก้ FilterPolicy)
    old = subscribers_table.get_item(Key={"email": email}).get("Item")
    old_arn = (old or {}).get("subscriptionArn", "")
    if old_arn.startswith("arn:aws:sns"):
        try:
            sns.set_subscription_attributes(
                SubscriptionArn=old_arn, AttributeName="FilterPolicy", AttributeValue=filter_policy
            )
            subscribers_table.update_item(
                Key={"email": email},
                UpdateExpression="SET province = :p",
                ExpressionAttributeValues={":p": province},
            )
            return respond(200, {"message": f"เปลี่ยนจังหวัดเป็น {province} เรียบร้อยแล้ว"})
        except sns.exceptions.NotFoundException:
            pass  # subscription ถูกลบไปแล้ว (เช่น กด unsubscribe) -> สมัครใหม่ด้านล่าง
        except sns.exceptions.InvalidParameterException:
            pass  # ยังไม่ได้ confirm

    try:
        resp = sns.subscribe(
            TopicArn=TOPIC_ARN,
            Protocol="email",
            Endpoint=email,
            Attributes={"FilterPolicy": filter_policy},
            ReturnSubscriptionArn=True,
        )
    except sns.exceptions.InvalidParameterException as e:
        # มักเกิดเมื่อสมัครซ้ำด้วยจังหวัดอื่น ขณะที่อันเดิมยังไม่ได้กด Confirm
        print(f"[WARN] subscribe ไม่สำเร็จ: {e}")
        return respond(409, {"error": "อีเมลนี้สมัครไว้แล้วแต่ยังไม่ได้กด Confirm กรุณากดยืนยันในอีเมลก่อน แล้วค่อยเปลี่ยนจังหวัด"})
    subscribers_table.put_item(Item={
        "email": email,
        "province": province,
        "subscriptionArn": resp.get("SubscriptionArn", "pending confirmation"),
        "createdAt": datetime.now(TH_TZ).isoformat(timespec="seconds"),
    })
    return respond(200, {"message": "เช็กอีเมลแล้วกด Confirm subscription (ดูในโฟลเดอร์ spam ด้วย)"})


def lambda_handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    # ตัด stage ออก ถ้ามี (เช่น /prod/readings)
    route = "/" + path.rstrip("/").split("/")[-1] if path != "/" else "/"

    try:
        if method == "OPTIONS":
            return respond(204, {})
        if method == "GET" and route == "/provinces":
            return get_provinces()
        if method == "GET" and route == "/readings":
            return get_readings()
        if method == "POST" and route == "/subscribe":
            return subscribe(event)
        return respond(404, {"error": f"ไม่พบ {method} {path}"})
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return respond(500, {"error": "ระบบขัดข้อง ลองใหม่อีกครั้ง"})
