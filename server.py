"""
SLAP BATTLES - Server
FastAPI + WebSockets
Run: uvicorn server:app --reload --host 0.0.0.0 --port 8000
"""
import os, asyncio, json, math, random, time, uuid
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PLATFORM_RADIUS = 35.0
FALL_THRESHOLD  = -8.0
TICK_RATE       = 0.05
GRAVITY         = -22.0
GROUND_Y        = 0.0
PLAYER_SPEED    = 2.0
JUMP_VEL        = 10.0
LOBBY_MAX       = 12
AI_TICK         = 0.2
RESPAWN_TIME    = 3.0
SLAP_COOLDOWN   = 0.6

COLORS = ["#e74c3c","#f39c12","#2ecc71","#3498db","#9b59b6",
          "#1abc9c","#e67e22","#e91e8c","#00bcd4","#cddc39","#ff5722","#607d8b"]
BOT_NAMES = ["SlappyBot","KingSlap","BotHand","SlapMaster","HandBot",
             "SlimBot","BotZap","FistBot","SlappyJr","HandSlam"]

GLOVES = {
    "default": {"name":"Default","cost":0,"color":"#ffffff","power":14.0,"speed_mult":1.0,"range":3.5,"ability":"none","desc":"A trusty white glove.","icon":"🤚"},
    "bomb":    {"name":"Bomb",   "cost":100,"color":"#111111","power":22.0,"speed_mult":0.85,"range":4.0,"ability":"explosion","desc":"Huge explosion knockback!","icon":"💥"},
    "speed":   {"name":"Speed",  "cost":150,"color":"#ffdd00","power":10.0,"speed_mult":1.7,"range":3.5,"ability":"dash","desc":"Super fast movement!","icon":"⚡"},
    "wave":    {"name":"Wave",   "cost":200,"color":"#4d96ff","power":16.0,"speed_mult":1.0,"range":6.0,"ability":"shockwave","desc":"Hits everyone nearby!","icon":"🌊"},
    "magnet":  {"name":"Magnet", "cost":250,"color":"#e74c3c","power":12.0,"speed_mult":0.9,"range":8.0,"ability":"pull","desc":"Pulls enemies to the edge!","icon":"🧲"},
    "inferno": {"name":"Inferno","cost":350,"color":"#ff6b00","power":18.0,"speed_mult":1.1,"range":3.5,"ability":"fire","desc":"Leaves fire on impact!","icon":"🔥"},
    "ghost":   {"name":"Ghost",  "cost":450,"color":"#c0c0c0","power":20.0,"speed_mult":1.2,"range":3.5,"ability":"teleport","desc":"Teleports behind enemy!","icon":"👻"},
    "tornado": {"name":"Tornado","cost":600,"color":"#9b59b6","power":15.0,"speed_mult":1.0,"range":5.0,"ability":"spin","desc":"Spin hits ALL players!","icon":"🌪️"},
    "admin": {"name":"GOD HAND","cost":0,"color":"#ffd700","power":999.0,"speed_mult":2.0,"range":20.0,"ability":"explosion","desc":"One slap = instant death!","icon":"☠️"},
}

DATABASE_URL = os.environ.get("DATABASE_URL","")
lobbies: Dict[str,"Lobby"] = {}
accounts: Dict[str,dict] = {}

def get_db():
    if not DATABASE_URL: return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    except Exception as e:
        print(f"DB error: {e}"); return None

def init_db():
    conn = get_db()
    if not conn:
        print("No DATABASE_URL — using JSON fallback"); return
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS sb_accounts (
            username TEXT PRIMARY KEY, password TEXT NOT NULL,
            slap_points INTEGER DEFAULT 0, total_slaps INTEGER DEFAULT 0,
            owned_gloves TEXT DEFAULT 'default', equipped_glove TEXT DEFAULT 'default')""")
        conn.commit()
        cur.execute("SELECT username,password,slap_points,total_slaps,owned_gloves,equipped_glove FROM sb_accounts")
        for row in cur.fetchall():
            accounts[row[0]]={"password":row[1],"slap_points":row[2],"total_slaps":row[3],
                              "owned_gloves":row[4].split(","),"equipped_glove":row[5]}
        print(f"Loaded {len(accounts)} accounts from DB")
        cur.close(); conn.close()
    except Exception as e:
        print(f"DB init error: {e}")

def save_account(u):
    if u not in accounts: return
    d = accounts[u]
    conn = get_db()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""INSERT INTO sb_accounts (username,password,slap_points,total_slaps,owned_gloves,equipped_glove)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (username) DO UPDATE SET
                password=EXCLUDED.password,slap_points=EXCLUDED.slap_points,
                total_slaps=EXCLUDED.total_slaps,owned_gloves=EXCLUDED.owned_gloves,
                equipped_glove=EXCLUDED.equipped_glove""",
                (u,d["password"],d["slap_points"],d["total_slaps"],",".join(d["owned_gloves"]),d["equipped_glove"]))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            print(f"DB save error: {e}")
    else:
        try:
            with open("sb_accounts.json","w") as f: json.dump(accounts,f)
        except: pass

def load_json():
    global accounts
    try:
        with open("sb_accounts.json","r") as f:
            accounts = json.load(f)
        for u,d in accounts.items():
            if "owned_gloves" not in d: d["owned_gloves"]=["default"]
            if "equipped_glove" not in d: d["equipped_glove"]="default"
            if "slap_points" not in d: d["slap_points"]=0
            if "total_slaps" not in d: d["total_slaps"]=0
        print(f"Loaded {len(accounts)} accounts from JSON")
    except FileNotFoundError:
        accounts={}

if DATABASE_URL: init_db()
else: load_json()

class Player:
    def __init__(self,pid,name,color,is_ai=False):
        self.id=pid; self.name=name; self.color=color; self.is_ai=is_ai; self.ws=None
        self.x=0.0; self.y=GROUND_Y; self.z=0.0
        self.vx=0.0; self.vy=0.0; self.vz=0.0; self.rot_y=0.0
        self.alive=True; self.on_ground=True
        self.last_slap=0.0; self.slaps=0; self.deaths=0
        self.respawn_at=0.0; self.glove="default"
        self.ai_target=None; self.ai_next=0.0

    def spawn(self):
        angle=random.uniform(0,math.pi*2); r=random.uniform(2,PLATFORM_RADIUS*0.55)
        self.x=math.cos(angle)*r; self.y=GROUND_Y; self.z=math.sin(angle)*r
        self.vx=0;self.vy=0;self.vz=0;self.alive=True;self.on_ground=True

    def to_dict(self):
        return {"id":self.id,"name":self.name,"color":self.color,"is_ai":self.is_ai,
                "x":self.x,"y":self.y,"z":self.z,"vx":self.vx,"vy":self.vy,"vz":self.vz,
                "rot_y":self.rot_y,"alive":self.alive,"slaps":self.slaps,
                "glove":self.glove,"respawn_at":self.respawn_at}

class Lobby:
    def __init__(self,lid,host,bot_mode=False):
        self.id=lid;self.host=host;self.bot_mode=bot_mode
        self.players:Dict[str,Player]={}
        self.effects=[];self.chat=[]
        self._task=None;self._running=True;self._last_tick=time.time()

    def add_player(self,p): self.players[p.id]=p
    def remove_player(self,pid): self.players.pop(pid,None)
    def alive_players(self): return [p for p in self.players.values() if p.alive]
    def color_for_slot(self):
        used={p.color for p in self.players.values()}
        for c in COLORS:
            if c not in used: return c
        return random.choice(COLORS)

    async def broadcast(self,msg):
        dead=[]
        for p in self.players.values():
            if p.ws and not p.is_ai:
                try: await p.ws.send_json(msg)
                except: dead.append(p.id)
        for pid in dead: self.remove_player(pid)

    async def send_state(self):
        now=time.time()
        clean=[e for e in self.effects if now-e.get("t",0)<1.5]
        self.effects=clean
        await self.broadcast({"type":"game_state",
            "players":[p.to_dict() for p in self.players.values()],
            "effects":self.effects})

    async def send_chat(self,sender,msg,system=False):
        e={"sender":sender,"msg":msg,"system":system,"ts":time.time()}
        self.chat.append(e)
        await self.broadcast({"type":"chat",**e})

    async def start_loop(self):
        self._task=asyncio.create_task(self._game_loop())

    async def _game_loop(self):
        if self.bot_mode: self._add_bots()
        await self.send_chat("","🖐️ Slap people off for +10 points!",system=True)
        while self._running:
            now=time.time()
            dt=min(now-self._last_tick,0.1)
            self._last_tick=now
            for p in list(self.players.values()):
                if not p.alive and now>=p.respawn_at:
                    p.spawn()
            for p in list(self.players.values()):
                if p.is_ai and p.alive: self._ai_tick(p,now)
            self._physics_tick(dt)
            for p in list(self.players.values()):
                if p.alive:
                    dist=math.sqrt(p.x**2+p.z**2)
                    # Check if on a bridge (4 bridges at 0, 90, 180, 270 degrees)
                    on_bridge = False
                    bridge_angles = [0, math.pi/2, math.pi, math.pi*1.5]
                    for ba in bridge_angles:
                        bx = math.cos(ba); bz = math.sin(ba)
                        # Project player position onto bridge direction
                        proj = p.x*bx + p.z*bz
                        perp = abs(-p.x*bz + p.z*bx)
                        if proj > PLATFORM_RADIUS and proj < PLATFORM_RADIUS+20 and perp < 2.0:
                            on_bridge = True
                            break
                    if p.y<FALL_THRESHOLD or (dist>PLATFORM_RADIUS+4 and not on_bridge):
                        p.alive=False;p.deaths+=1;p.respawn_at=now+RESPAWN_TIME
                        await self.send_chat("",f"💨 {p.name} got slapped off!",system=True)
            await self.send_state()
            await asyncio.sleep(TICK_RATE)

    def _add_bots(self):
        existing=[p for p in self.players.values() if p.is_ai]
        for b in existing: self.remove_player(b.id)
        for i in range(7):
            name=BOT_NAMES[i%len(BOT_NAMES)]+str(random.randint(1,99))
            pid="bot_"+str(uuid.uuid4())[:5]
            bot=Player(pid,name,self.color_for_slot(),is_ai=True)
            bot.glove=random.choice([g for g in GLOVES.keys() if g != "admin"])
            bot.spawn(); self.add_player(bot)

    def _physics_tick(self,dt):
        for p in self.players.values():
            if not p.alive: continue
            if not p.on_ground: p.vy+=GRAVITY*dt
            p.x+=p.vx*dt;p.y+=p.vy*dt;p.z+=p.vz*dt
            dist=math.sqrt(p.x**2+p.z**2)
            if p.y<=GROUND_Y and dist<PLATFORM_RADIUS:
                p.y=GROUND_Y;p.vy=0;p.on_ground=True
            else: p.on_ground=False
            fr=0.78 if p.on_ground else 0.97
            p.vx*=fr;p.vz*=fr

    def _do_slap(self,attacker,aim_angle,power_mult=1.0):
        now=time.time()
        if now-attacker.last_slap<SLAP_COOLDOWN: return
        g=GLOVES[attacker.glove]
        attacker.last_slap=now
        base_power=g["power"]*power_mult
        ability=g["ability"]
        if ability in ("shockwave","spin"):
            targets=[p for p in self.alive_players() if p.id!=attacker.id and math.sqrt((p.x-attacker.x)**2+(p.z-attacker.z)**2)<g["range"]]
        else:
            best=None;best_dist=g["range"]
            for p in self.alive_players():
                if p.id==attacker.id: continue
                d=math.sqrt((p.x-attacker.x)**2+(p.z-attacker.z)**2)
                if d<best_dist: best_dist=d;best=p
            targets=[best] if best else []
        hits=0
        for target in targets:
            dx=target.x-attacker.x;dz=target.z-attacker.z
            dist=math.sqrt(dx**2+dz**2)
            if dist==0: dx,dz,dist=1,0,1
            else: dx/=dist;dz/=dist
            if ability=="pull":
                td=math.sqrt(target.x**2+target.z**2)
                if td>0: target.vx+=(target.x/td)*base_power*1.3;target.vz+=(target.z/td)*base_power*1.3
            elif ability=="explosion":
                target.vx+=dx*base_power*1.5;target.vz+=dz*base_power*1.5;target.vy+=9
            else:
                target.vx+=dx*base_power;target.vz+=dz*base_power;target.vy+=5
            target.on_ground=False;hits+=1
        if hits>0:
            attacker.slaps+=hits
            if not attacker.is_ai and attacker.name in accounts:
                accounts[attacker.name]["slap_points"]+=10*hits
                accounts[attacker.name]["total_slaps"]+=hits
                save_account(attacker.name)
        self.effects.append({"type":ability if ability!="none" else "slap","x":attacker.x,"z":attacker.z,"t":time.time()})
        if ability=="teleport":
            enemies=[p for p in self.alive_players() if p.id!=attacker.id]
            if enemies:
                tgt=min(enemies,key=lambda p:math.sqrt((p.x-attacker.x)**2+(p.z-attacker.z)**2))
                attacker.x=tgt.x-math.sin(tgt.rot_y)*1.5;attacker.z=tgt.z-math.cos(tgt.rot_y)*1.5

    def _ai_tick(self,ai,now):
        if now<ai.ai_next: return
        ai.ai_next=now+AI_TICK+random.uniform(0,0.15)
        enemies=[p for p in self.alive_players() if p.id!=ai.id]
        if not enemies: return
        def d(p): return math.sqrt((p.x-ai.x)**2+(p.z-ai.z)**2)
        target=min(enemies,key=d)
        dx=target.x-ai.x;dz=target.z-ai.z;dist=d(target)
        my_dist=math.sqrt(ai.x**2+ai.z**2)
        if my_dist>PLATFORM_RADIUS*0.8:
            ai.vx+=(-ai.x/my_dist)*5.0*0.7;ai.vz+=(-ai.z/my_dist)*5.0*0.7;return
        g=GLOVES[ai.glove]
        if dist<g["range"]:
            ai.rot_y=math.atan2(dx,dz);self._do_slap(ai,ai.rot_y,random.uniform(0.6,1.0))
        elif dist>0:
            s=g["speed_mult"]*5.0*random.uniform(0.8,1.0)
            ai.vx+=(dx/dist)*s*AI_TICK;ai.vz+=(dz/dist)*s*AI_TICK;ai.rot_y=math.atan2(dx,dz)

@app.get("/")
async def root(): return FileResponse("static/index.html")

@app.get("/gloves")
async def get_gloves(): return GLOVES

@app.get("/lobbies")
async def list_lobbies():
    return [{"id":lid,"host":lb.host,"players":len(lb.players),"max":LOBBY_MAX,"bot_mode":lb.bot_mode} for lid,lb in lobbies.items()]

@app.post("/register")
async def register(data:dict=Body(...)):
    u=str(data.get("username","")).strip()[:16];p=str(data.get("password","")).strip()
    if not u or not p: return {"ok":False,"msg":"Fill in all fields!"}
    if len(p)<4: return {"ok":False,"msg":"Password 4+ chars"}
    if u in accounts: return {"ok":False,"msg":"Username taken!"}
    accounts[u]={"password":p,"slap_points":0,"total_slaps":0,"owned_gloves":["default"],"equipped_glove":"default"}
    save_account(u)
    return {"ok":True,"username":u,"slap_points":0,"total_slaps":0,"owned_gloves":["default"],"equipped_glove":"default"}

@app.post("/login")
async def login(data:dict=Body(...)):
    u=str(data.get("username","")).strip()[:16];p=str(data.get("password","")).strip()
    if u not in accounts: return {"ok":False,"msg":"Account not found!"}
    if accounts[u]["password"]!=p: return {"ok":False,"msg":"Wrong password!"}
    d=accounts[u]
    return {"ok":True,"username":u,"slap_points":d["slap_points"],"total_slaps":d["total_slaps"],"owned_gloves":d["owned_gloves"],"equipped_glove":d["equipped_glove"]}

@app.post("/buy_glove")
async def buy_glove(data:dict=Body(...)):
    u=str(data.get("username","")).strip();gid=str(data.get("glove_id","")).strip()
    if u not in accounts: return {"ok":False,"msg":"Not logged in!"}
    if gid not in GLOVES: return {"ok":False,"msg":"Invalid glove!"}
    acc=accounts[u];g=GLOVES[gid]
    if gid in acc["owned_gloves"]: return {"ok":False,"msg":"Already owned!"}
    if acc["slap_points"]<g["cost"]: return {"ok":False,"msg":f"Need {g['cost']} pts (you have {acc['slap_points']})"}
    acc["slap_points"]-=g["cost"];acc["owned_gloves"].append(gid);save_account(u)
    return {"ok":True,"slap_points":acc["slap_points"],"owned_gloves":acc["owned_gloves"]}

@app.post("/equip_glove")
async def equip_glove(data:dict=Body(...)):
    u=str(data.get("username","")).strip();gid=str(data.get("glove_id","")).strip()
    if u not in accounts: return {"ok":False,"msg":"Not logged in!"}
    if gid not in accounts[u]["owned_gloves"]: return {"ok":False,"msg":"Don't own this!"}
    accounts[u]["equipped_glove"]=gid;save_account(u)
    return {"ok":True,"equipped_glove":gid}

@app.websocket("/ws/{lobby_id}/{player_name}/{bot_mode}")
async def ws_endpoint(ws:WebSocket,lobby_id:str,player_name:str,bot_mode:str):
    await ws.accept()
    is_bot=bot_mode=="bots"
    if lobby_id not in lobbies: lobbies[lobby_id]=Lobby(lobby_id,player_name,bot_mode=is_bot)
    lobby=lobbies[lobby_id]
    if len(lobby.players)>=LOBBY_MAX:
        await ws.send_json({"type":"error","msg":"Lobby full!"}); await ws.close(); return
    pid=str(uuid.uuid4())[:8];color=lobby.color_for_slot()
    player=Player(pid,player_name[:16],color);player.ws=ws
    if player_name in accounts: player.glove=accounts[player_name].get("equipped_glove","default")
    lobby.add_player(player);player.spawn()
    await ws.send_json({"type":"joined","your_id":pid})
    await lobby.send_chat("",f"🖐️ {player_name} joined!",system=True)
    await lobby.send_state()
    if lobby._task is None or lobby._task.done(): await lobby.start_loop()
    try:
        while True:
            data=await ws.receive_json()
            await _handle(lobby,player,data)
    except (WebSocketDisconnect,Exception):
        lobby.remove_player(pid)
        await lobby.send_chat("",f"👋 {player_name} left.",system=True)
        if len([p for p in lobby.players.values() if not p.is_ai])==0:
            lobby._running=False;lobbies.pop(lobby_id,None)
        else: await lobby.send_state()

async def _handle(lobby,player,data):
    kind=data.get("type")
    if kind=="input" and player.alive:
        inp=data.get("input",{})
        dx=float(inp.get("dx",0));dz=float(inp.get("dz",0))
        length=math.sqrt(dx**2+dz**2)
        if length>0:
            dx/=length;dz/=length
            g=GLOVES[player.glove];spd=PLAYER_SPEED*g["speed_mult"]
            player.vx+=dx*spd*TICK_RATE*10;player.vz+=dz*spd*TICK_RATE*10
        aim=inp.get("aim_angle")
        if aim is not None: player.rot_y=float(aim)
        elif length>0: player.rot_y=math.atan2(dx,dz)
        if inp.get("slap"):
            power=float(inp.get("power",1.0));aim_a=float(inp.get("aim_angle") or player.rot_y)
            lobby._do_slap(player,aim_a,power)
    elif kind=="chat":
        msg=str(data.get("msg",""))[:200].strip()
        if msg: await lobby.send_chat(player.name,msg)
    elif kind=="equip":
        gid=str(data.get("glove_id",""))
        if gid in GLOVES and player.name in accounts and gid in accounts[player.name]["owned_gloves"]:
            player.glove=gid;accounts[player.name]["equipped_glove"]=gid;save_account(player.name)

app.mount("/static",StaticFiles(directory="static"),name="static")
