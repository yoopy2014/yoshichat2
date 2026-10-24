import os
import sqlite3
from typing import List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# データベースの初期化
def init_db():
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    
    # ユーザーテーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            yoshi_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            password TEXT NOT NULL
        )
    """)
    
    # フレンド関係テーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS friends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id TEXT NOT NULL,
            receiver_id TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """)

    # メッセージ履歴テーブル
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT NOT NULL,
            receiver_id TEXT NOT NULL,
            content TEXT NOT NULL,
            is_stamp INTEGER NOT NULL DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()

init_db()

# リアルタイム通信用の接続管理クラス
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, yoshi_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[yoshi_id] = websocket

    def disconnect(self, yoshi_id: str):
        if yoshi_id in self.active_connections:
            del self.active_connections[yoshi_id]

    async def send_personal_message(self, message: dict, yoshi_id: str):
        if yoshi_id in self.active_connections:
            await self.active_connections[yoshi_id].send_json(message)

manager = ConnectionManager()

# リクエストデータの定義
class UserRegister(BaseModel):
    yoshi_id: str
    name: str
    password: str

class UserLogin(BaseModel):
    yoshi_id: str
    password: str

class FriendRequest(BaseModel):
    my_id: str
    target_id: str

# API: ユーザー登録
@app.post("/api/register")
def register(user: UserRegister):
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (yoshi_id, name, password) VALUES (?, ?, ?)",
                       (user.yoshi_id, user.name, user.password))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="このyoshiIDは既に使用されています")
    conn.close()
    return {"status": "ok", "user": {"yoshiID": user.yoshi_id, "name": user.name}}

# API: ログイン
@app.post("/api/login")
def login(user: UserLogin):
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("SELECT yoshi_id, name FROM users WHERE yoshi_id = ? AND password = ?",
                   (user.yoshi_id, user.password))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=400, detail="yoshiIDまたはパスワードが違います")
    return {"status": "ok", "user": {"yoshiID": row[0], "name": row[1]}}

# API: フレンド申請
@app.post("/api/request_friend")
async def request_friend(req: FriendRequest):
    if req.my_id == req.target_id:
        raise HTTPException(status_code=400, detail="自分自身には申請できません")
        
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    
    cursor.execute("SELECT yoshi_id FROM users WHERE yoshi_id = ?", (req.target_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="指定されたユーザーが存在しません")

    cursor.execute("""
        SELECT status FROM friends 
        WHERE (requester_id = ? AND receiver_id = ?) 
           OR (requester_id = ? AND receiver_id = ?)
    """, (req.my_id, req.target_id, req.target_id, req.my_id))
    existing = cursor.fetchone()
    
    if existing:
        conn.close()
        raise HTTPException(status_code=400, detail="既に申請中またはフレンドです")

    cursor.execute("INSERT INTO friends (requester_id, receiver_id, status) VALUES (?, ?, 'pending')",
                   (req.my_id, req.target_id))
    conn.commit()
    conn.close()

    await manager.send_personal_message({"type": "friend_request_received"}, req.target_id)
    return {"status": "ok"}

# API: フレンド承認
@app.post("/api/accept_friend")
async def accept_friend(req: FriendRequest):
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE friends SET status = 'accepted' 
        WHERE requester_id = ? AND receiver_id = ? AND status = 'pending'
    """, (req.target_id, req.my_id))
    
    if cursor.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=400, detail="承認対象の申請が見つかりません")
        
    conn.commit()
    conn.close()

    await manager.send_personal_message({"type": "friend_request_accepted"}, req.target_id)
    return {"status": "ok"}

# API: 届いているフレンド申請一覧を取得
@app.get("/api/friend_requests/{yoshi_id}")
def get_friend_requests(yoshi_id: str):
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.yoshi_id, u.name FROM friends f
        JOIN users u ON f.requester_id = u.yoshi_id
        WHERE f.receiver_id = ? AND f.status = 'pending'
    """, (yoshi_id,))
    rows = cursor.fetchall()
    conn.close()
    return [{"yoshiID": row[0], "name": row[1]} for row in rows]

# API: フレンド一覧を取得
@app.get("/api/friends/{yoshi_id}")
def get_friends(yoshi_id: str):
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.yoshi_id, u.name FROM friends f
        JOIN users u ON (CASE WHEN f.requester_id = ? THEN f.receiver_id ELSE f.requester_id END) = u.yoshi_id
        WHERE (f.requester_id = ? OR f.receiver_id = ?) AND f.status = 'accepted'
    """, (yoshi_id, yoshi_id, yoshi_id))
    rows = cursor.fetchall()
    conn.close()
    return [{"yoshiID": row[0], "name": row[1]} for row in rows]

# API: メッセージ履歴を取得
@app.get("/api/messages/{my_id}/{target_id}")
def get_messages(my_id: str, target_id: str):
    conn = sqlite3.connect("chat.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sender_id, receiver_id, content, is_stamp FROM messages
        WHERE (sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?)
        ORDER BY id ASC
    """, (my_id, target_id, target_id, my_id))
    rows = cursor.fetchall()
    conn.close()
    return [{
        "sender_id": row[0],
        "receiver_id": row[1],
        "content": row[2],
        "is_stamp": bool(row[3])
    } for row in rows]

# WebSocket エンドポイント
@app.websocket("/ws/{yoshi_id}")
async def websocket_endpoint(websocket: WebSocket, yoshi_id: str):
    await manager.connect(yoshi_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            target_id = data.get("to")
            content = data.get("content")
            is_stamp = data.get("is_stamp", False)
            
            if target_id and content:
                conn = sqlite3.connect("chat.db")
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO messages (sender_id, receiver_id, content, is_stamp) VALUES (?, ?, ?, ?)",
                    (yoshi_id, target_id, content, 1 if is_stamp else 0)
                )
                conn.commit()
                conn.close()

                await manager.send_personal_message({
                    "type": "message",
                    "from": yoshi_id,
                    "content": content,
                    "is_stamp": is_stamp
                }, target_id)
    except WebSocketDisconnect:
        manager.disconnect(yoshi_id)

# 静くファイルの配信（一番最後に記述）
app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)