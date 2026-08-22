# KICK Direct Live — Full Phase 1

ระบบต้นแบบสำหรับ:

**KICK OAuth 2.1 + PKCE → อ่านบัญชี/ช่อง → อ่าน Stream URL/Stream Key → FFmpeg → RTMPS → KICK Live**

ไม่มี OBS เป็นตัวกลาง

## สิ่งที่ Phase 1 ทำ

- Login ด้วย KICK OAuth 2.1 + PKCE
- อ่านผู้ใช้ที่ authorize แอป
- อ่านข้อมูล channel
- ใช้สิทธิ์ `streamkey:read` เพื่อเตรียมอ่าน Stream URL/Stream Key
- สร้างวิดีโอ/เสียงทดสอบด้วย FFmpeg
- Encode เป็น H.264 + AAC
- ส่งผ่าน RTMPS ตรงไปยัง KICK
- Start / Stop / Status จากหน้าเว็บ
- Dockerfile สำหรับ Render พร้อม FFmpeg
- ใช้ environment variables สำหรับความลับ ไม่เก็บ Client Secret/Stream Key ใน GitHub

KICK ระบุ `streamkey:read` ว่าใช้สำหรับอ่าน Stream URL และ Stream Key และ Public API มี OAuth, Users, Channels และ Livestreams endpoints. ดูเอกสาร KICK Developer ที่ https://dev.kick.com/ และ Swagger ที่ https://api.kick.com/swagger/index.html

## สำคัญเกี่ยวกับ Stream Key endpoint

สิทธิ์ `streamkey:read` เป็นสิทธิ์ทางการของ KICK แต่ชื่อ path ของ endpoint ดึง stream credential อาจเปลี่ยนตาม API revision ได้ โค้ดจึงรวมจุดนี้ไว้ใน `get_stream_credentials()` เพื่อให้อัปเดต path ได้โดยไม่ต้องแก้ส่วน Live ทั้งระบบ

ถ้า KICK API ของบัญชีคุณไม่ตอบที่ path ที่ลองไว้ ระบบจะแจ้งว่า `stream_key_unavailable` แทนการทำให้แอปพังหรือแสดง credential ออกมา

## 1. สร้าง KICK Developer App

1. ไปที่ KICK Developer
2. สร้าง App
3. ตั้ง Redirect URI ให้ตรงกับ deployment ของคุณ เช่น:

Local:

`http://127.0.0.1:5000/callback`

Render:

`https://YOUR-SERVICE.onrender.com/callback`

4. เปิด permissions อย่างน้อย:

- `user:read`
- `channel:read`
- `streamkey:read`

Phase ต่อไปจึงค่อยเพิ่ม `channel:write`, `chat:write`, `events:subscribe`

## 2. Local

ต้องมี Python 3.12+ และ FFmpeg

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

เปิด:

`http://127.0.0.1:5000`

## 3. GitHub

```bash
git init
git add .
git commit -m "Initial KICK direct live"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/YOUR_REPO.git
git push -u origin main
```

อย่า commit `.env` เพราะ `.gitignore` กันไว้ให้แล้ว

## 4. Render

แนะนำสร้าง **Web Service จาก GitHub Repo** และเลือก Docker โดยใช้ `Dockerfile` ใน repo

Environment variables ที่ต้องตั้ง:

- `KICK_CLIENT_ID`
- `KICK_CLIENT_SECRET`
- `KICK_REDIRECT_URI`
- `APP_SECRET`
- `SESSION_COOKIE_SECURE=true`
- `KICK_RTMPS_URL` ถ้าคุณได้รับค่า ingest URL อื่นจาก KICK

`PORT` ไม่จำเป็นต้องกำหนดเอง เพราะ Dockerfile ฟังที่ 10000 และ Render จะ route ให้อัตโนมัติสำหรับ Web Service ที่ใช้ container นี้

## 5. ทดสอบจริง

1. เปิด URL Render
2. กด `เข้าสู่ระบบด้วย KICK`
3. อนุญาต permissions
4. กลับหน้า Control Panel
5. กด `START LIVE`
6. เปิดหน้า channel KICK ของบัญชีที่ authorize
7. ตรวจว่าช่องขึ้น LIVE และเห็นภาพ/เสียงทดสอบ

## 6. ถ้า Login สำเร็จแต่ Start Live ไม่ได้

ดูข้อความในหน้าเว็บและ log ของ Render โดยเฉพาะ:

- stream key permission ไม่ได้เปิด
- Redirect URI ไม่ตรง
- KICK App ยังไม่ได้รับสิทธิ์ที่ต้องการ
- RTMPS URL ไม่ตรงกับ ingest URL ที่ KICK ให้บัญชี
- FFmpeg/codec มีปัญหา

## 7. ทำไมใช้ workers=1

Phase 1 เก็บ FFmpeg process ไว้ใน memory ของ process เดียว จึงต้องใช้ Gunicorn worker เดียวก่อน ถ้าจะทำระบบ production ที่มี reconnect, queue, multi-user และ AI จะต้องแยก streaming worker ออกจาก web control plane

## 8. Phase ต่อไป

เมื่อยืนยันว่า KICK เห็น Live จากระบบนี้จริงแล้ว จึงต่อ:

```text
AI Avatar
   ↓
TTS
   ↓
Video/Audio Pipeline
   ↓
FFmpeg
   ↓
RTMPS
   ↓
KICK
```

แล้วจึงเพิ่ม:

```text
KICK Events / Chat
       ↓
     AI Brain
       ↓
     TTS / Chat
```

และ Donate:

```text
Donate Webhook
       ↓
   Real-time Event
       ↓
       AI
       ↓
  ตอบกลับบน Live
```

## Security

- ห้าม commit `.env`
- อย่าใส่ Stream Key ใน frontend
- ห้ามเขียน Stream Key ลง log
- ใช้ HTTPS บน Render
- ใช้ `SESSION_COOKIE_SECURE=true` บน production
- ขอ OAuth scopes เท่าที่จำเป็น
