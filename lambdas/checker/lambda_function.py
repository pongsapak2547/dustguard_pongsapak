"""
dustguard-checker
รันทุก 1 ชั่วโมงจาก EventBridge
  1. อ่าน config.json จาก S3
  2. ดึงค่าฝุ่นจาก Air4Thai -> เฉลี่ย PM2.5 ตามจังหวัด
  3. บันทึกลง DynamoDB (dustguard-readings)
  4. จังหวัดที่เกินเกณฑ์และพ้น cooldown -> ส่ง SNS (แยกจังหวัดด้วย MessageAttributes)
  5. เขียน log รายวันลง S3 (logs/YYYY-MM-DD.json)

โหมดทดสอบ: Test event {"test_threshold": 5} -> ใช้เกณฑ์ 5 และข้าม cooldown
"""
import json
import os
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

AIR4THAI_URL = "https://air4thai.pcd.go.th/services/getNewAQI_JSON.php"
TH_TZ = timezone(timedelta(hours=7))
MAX_DATA_AGE_HOURS = 6  # ข้ามสถานีที่ข้อมูลเก่ากว่านี้ (สถานีเสีย)

TABLE_READINGS = os.environ.get("TABLE_READINGS", "dustguard-readings")
BUCKET = os.environ.get("BUCKET", "")
TOPIC_ARN = os.environ.get("TOPIC_ARN", "")

s3 = boto3.client("s3")
sns = boto3.client("sns")
readings_table = boto3.resource("dynamodb").Table(TABLE_READINGS)

DEFAULT_CONFIG = {
    "threshold": 37.5,
    "provinces": ["กรุงเทพมหานคร"],
    "cooldownHours": 6,
}


# ---------- อ่านข้อมูล ----------

def load_config():
    try:
        obj = s3.get_object(Bucket=BUCKET, Key="config.json")
        return {**DEFAULT_CONFIG, **json.loads(obj["Body"].read().decode("utf-8"))}
    except Exception as e:
        print(f"[WARN] อ่าน config.json ไม่ได้ ใช้ค่าเริ่มต้นแทน: {e}")
        return DEFAULT_CONFIG


def fetch_air4thai():
    req = urllib.request.Request(AIR4THAI_URL, headers={"User-Agent": "DustGuard/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ssl.SSLCertVerificationError):
            raise
        # Air4Thai ส่งใบรับรอง SSL มาไม่ครบ ข้อมูลเป็นข้อมูลสาธารณะแบบอ่านอย่างเดียว จึงข้ามการตรวจเฉพาะเว็บนี้
        print("[WARN] ตรวจใบรับรอง SSL ของ Air4Thai ไม่ผ่าน ใช้การเชื่อมต่อแบบไม่ตรวจใบรับรองแทน")
        resp = urllib.request.urlopen(req, timeout=30, context=ssl._create_unverified_context())
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def province_of(station):
    """แยกชื่อจังหวัดจาก areaTH เช่น 'ต.ปากน้ำ อ.เมือง, กระบี่' -> 'กระบี่'"""
    area = (station.get("areaTH") or "").strip()
    if "," in area:
        prov = area.split(",")[-1].strip()
    elif station.get("stationType") == "BKK" or "เขต" in area:
        # สถานีของ กทม. บางแห่งไม่มี ', กรุงเทพฯ' ต่อท้าย
        prov = "กรุงเทพฯ"
    else:
        return None
    if prov.startswith("จ."):
        prov = prov[2:].strip()
    if prov in ("กรุงเทพฯ", "กรุงเทพ", "กทม."):
        prov = "กรุงเทพมหานคร"
    return prov


def pm25_of(station, now):
    """คืนค่า PM2.5 (float) หรือ None ถ้าไม่มีข้อมูล/ข้อมูลเก่าเกิน"""
    last = station.get("AQILast") or {}
    try:
        value = float((last.get("PM25") or {}).get("value", -1))
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    try:
        measured = datetime.strptime(f"{last['date']} {last['time']}", "%Y-%m-%d %H:%M").replace(tzinfo=TH_TZ)
        if now - measured > timedelta(hours=MAX_DATA_AGE_HOURS):
            return None
    except (KeyError, ValueError):
        pass  # ไม่มีวันที่ก็ยังใช้ค่าได้
    return value


def average_by_province(data, wanted, now):
    sums = {p: [0.0, 0] for p in wanted}
    for st in data.get("stations", []):
        prov = province_of(st)
        if prov not in sums:
            continue
        v = pm25_of(st, now)
        if v is None:
            continue
        sums[prov][0] += v
        sums[prov][1] += 1
    return {p: (round(total / n, 1), n) for p, (total, n) in sums.items() if n > 0}


# ---------- cooldown ----------

def last_alert_time(province):
    """หาเวลาที่ส่งเตือนล่าสุดของจังหวัดนี้ (ดูย้อนหลัง 48 รายการ)"""
    resp = readings_table.query(
        KeyConditionExpression=Key("province").eq(province),
        ScanIndexForward=False,
        Limit=48,
    )
    for item in resp.get("Items", []):
        if item.get("alerted") and not item.get("test"):  # ไม่นับรอบทดสอบ
            return datetime.fromisoformat(item["ts"])
    return None


# ---------- แจ้งเตือน ----------

def advice_for(pm25):
    if pm25 > 75:
        return ("ระดับ: มีผลกระทบต่อสุขภาพ (สีแดง)\n"
                "- ทุกคนควรงดกิจกรรมกลางแจ้ง\n"
                "- สวมหน้ากาก N95 ทุกครั้งที่ออกนอกอาคาร\n"
                "- ปิดประตูหน้าต่าง เปิดเครื่องฟอกอากาศถ้ามี\n"
                "- หากมีอาการไอ หายใจลำบาก แน่นหน้าอก ให้พบแพทย์")
    if pm25 > 37.5:
        return ("ระดับ: เริ่มมีผลกระทบต่อสุขภาพ (สีส้ม)\n"
                "- ลดหรืองดกิจกรรมกลางแจ้ง โดยเฉพาะการออกกำลังกาย\n"
                "- สวมหน้ากาก N95 เมื่อออกนอกอาคาร\n"
                "- เด็ก ผู้สูงอายุ หญิงตั้งครรภ์ และผู้มีโรคประจำตัว ควรอยู่ในอาคาร")
    if pm25 > 25:
        return ("ระดับ: ปานกลาง (สีเหลือง)\n"
                "- คนทั่วไปทำกิจกรรมกลางแจ้งได้ตามปกติ\n"
                "- กลุ่มเสี่ยง (เด็ก ผู้สูงอายุ หญิงตั้งครรภ์ ผู้มีโรคประจำตัว) ควรลดเวลาอยู่กลางแจ้ง\n"
                "- ถ้ามีอาการไอ ระคายเคืองตา ควรลดกิจกรรมกลางแจ้ง")
    if pm25 > 15:
        return ("ระดับ: ดี (สีเขียว)\n"
                "- ทำกิจกรรมกลางแจ้งได้ตามปกติ")
    return ("ระดับ: ดีมาก (สีฟ้า)\n"
            "- อากาศดีมาก เหมาะกับกิจกรรมกลางแจ้ง")


def send_alert(province, pm25, stations, threshold, ts):
    subject = f"⚠️ DustGuard: PM2.5 {province} {pm25} µg/m³"
    message = (
        f"แจ้งเตือนค่าฝุ่น PM2.5 จังหวัด{province}\n\n"
        f"ค่าเฉลี่ย PM2.5: {pm25} µg/m³ (จาก {stations} สถานี)\n"
        f"เกณฑ์แจ้งเตือน: {threshold} µg/m³\n"
        f"เวลา: {ts}\n\n"
        f"{advice_for(pm25)}\n\n"
        f"ข้อมูลจาก Air4Thai กรมควบคุมมลพิษ\n"
        f"— DustGuard ระบบแจ้งเตือนฝุ่นอัตโนมัติ"
    )
    sns.publish(
        TopicArn=TOPIC_ARN,
        Subject=subject[:100],  # SNS จำกัด Subject 100 ตัวอักษร
        Message=message,
        MessageAttributes={"province": {"DataType": "String", "StringValue": province}},
    )


# ---------- log ----------

def append_daily_log(now, entry):
    key = f"logs/{now.strftime('%Y-%m-%d')}.json"
    try:
        logs = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8"))
    except s3.exceptions.NoSuchKey:
        logs = []
    except Exception as e:
        print(f"[WARN] อ่าน log เดิมไม่ได้: {e}")
        logs = []
    logs.append(entry)
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(logs, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


# ---------- main ----------

def lambda_handler(event, context):
    event = event or {}
    now = datetime.now(TH_TZ).replace(second=0, microsecond=0)
    ts = now.isoformat()

    config = load_config()
    test_mode = "test_threshold" in event
    threshold = float(event["test_threshold"]) if test_mode else float(config["threshold"])
    cooldown = timedelta(hours=float(config.get("cooldownHours", 6)))
    provinces = config["provinces"]

    data = fetch_air4thai()
    stations = data.get("stations", [])
    print(f"[INFO] ได้ข้อมูล {len(stations)} สถานี, ทดสอบ={test_mode}, เกณฑ์={threshold}")
    if stations:
        print("[DEBUG] ตัวอย่างสถานีแรก:", json.dumps(stations[0], ensure_ascii=False)[:800])

    averages = average_by_province(data, provinces, now)
    missing = [p for p in provinces if p not in averages]
    if missing:
        print(f"[WARN] ไม่มีข้อมูลของจังหวัด: {missing}")

    results, alerts_sent = [], 0
    for province, (pm25, n) in averages.items():
        alerted = False
        if pm25 > threshold:
            last = None if test_mode else last_alert_time(province)
            if last is None or now - last >= cooldown:
                send_alert(province, pm25, n, threshold, ts)
                alerted = True
                alerts_sent += 1
            else:
                print(f"[INFO] {province} เกินเกณฑ์แต่ยังอยู่ใน cooldown (ส่งล่าสุด {last.isoformat()})")

        readings_table.put_item(Item={
            "province": province,
            "ts": ts,
            "pm25": Decimal(str(pm25)),
            "stations": n,
            "alerted": alerted,
            "test": test_mode,
        })
        results.append({"province": province, "pm25": pm25, "stations": n, "alerted": alerted})
        print(f"[INFO] {province}: {pm25} µg/m³ ({n} สถานี) แจ้งเตือน={alerted}")

    append_daily_log(now, {
        "ts": ts,
        "test_mode": test_mode,
        "threshold": threshold,
        "results": results,
        "missing": missing,
    })

    return {"statusCode": 200, "provinces_checked": len(results), "alerts_sent": alerts_sent, "results": results}
