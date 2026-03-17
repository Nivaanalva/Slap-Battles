"""
Slap Battles Server
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

PLATFORM_RADIUS = 14.0
FALL_THRESHOLD  = -5.0
TICK_RATE       = 0.05
SLAP_RANGE      = 3.0
SLAP_COOLDOWN   = 0.7
GRAVITY         = -22.0
GROUND_Y        = 0.0
PLAYER_SPEED    = 5.0
JUMP_VEL        = 9.0
RESPAWN_TIME    = 3.0
LOBBY_MAX       = 9
AI_TICK         = 0.3
POINTS_PER_SLAP = 10

DATABASE_URL = os.environ.get("DATABASE_URL", "")

GLOVES = {
    "default":  {"name":"Default 👊",  "cost":0,    "color":"#ff6b6b", "desc":"Classic slap"},
    "speed":    {"name":"Speed ⚡",     "cost":50,   "color":"#ffd93d", "desc":"Move 2x faster"},
    "bomb":     {"name":"Bomb 💥",      "cost":100,  "color":"#ff4757", "desc":"Massive knockback"},
    "wave":     {"name":"Wave 🌊",      "cost":150,  "color":"#4d96ff", "desc":"Shockwave hits everyone nearby"},
    "magnet":   {"name":"Magnet 🧲",    "cost":200,  "color":"#c77dff", "desc":"Pulls enemies to edge"},
    "inferno":  {"name":"Inferno 🔥",   "cost":300,  "color":"#ff9a3c", "desc":"Extra knockback + fire trail"},
    "ghost":    {"name":"Ghost 👻",     "cost":400,  "color":"#a8d8ea", "desc":"Teleport behind enemy"},
    "tornado":  {"name":"Tornado 🌪️",  "cost":500,  "color":"#6bcb77", "desc":"Spin hits everyone around you"},
    "overkill": {"name":"Overkill ☠️", "cost":1000, "color":"#1a1a2e", "desc":"ONE SHOT — instant off map"},
}

COLORS = ["#FF6B6B","#FFD93D","#6BCB77","#4D96FF","#C77DFF","#FF9A3C","#00C9A7","#FF61A6"]
BOT_NAMES = ["BotSlapper","BotKing","BotZap","BotRex","BotNova","BotBash","BotPunch","BotSlam"]

accounts: Dict[str, dict] = {}

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
        print("No DB — in-memory only"); return
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS slap_accounts (
            username TEXT PRIMARY KEY, password TEXT NOT NULL,
            points INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
            unlocked TEXT DEFAULT 'default')""")
        conn.commit()
        cur.execute("SELECT username,password,points,wins,unlocked FROM slap_accounts")
        for row in cur.fetchall():
            accounts[row[0]] = {"password":row[1],"points":row[2],"wins":row[3],"unlocked":row[4].split(",")}
        print(f"Loaded {len(accounts)} accounts from DB")
        cur.close(); conn.close()
    except Exception as e:
        print(f"DB init error: {e}")

def save_account(u):
    conn = get_db()
    if not conn: return
    try:
        d = accounts[u]; cur = conn.cursor()
        cur.execute("""INSERT INTO slap_accounts (username,password,points,wins,unlocked)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO UPDATE
            SET password=EXCLUDED.password,points=EXCLUDED.points,
                wins=EXCLUDED.wins,unlocked=EXCLUDED.unlocked""",
            (u,d["password"],d["points"],d["wins"],",".join(d["unlocked"])))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"DB save error: {e}")

init_db()

class Player:
    def __init__(self, pid, name, color, is_ai=False):
        self.id=pid; self.name=name; self.color=color; self.is_ai=is_ai
        self.ws=None
        self.x=self.y=self.z=0.0
        self.vx=self.vy=self.vz=0.0
        self.rot_y=0.0; self.on_ground=True; self.alive=True
        self.respawn_at=0.0; self.slap_cd=0.0
        self.glove="default"; self.session_points=0; self.kills=0
        self.fire_trail=[]; self.ai_next=0.0

    def spawn(self, index, total):
        angle=(2*math.pi*index)/max(total,1)
        r=PLATFORM_RADIUS*0.5
        self.x=r*math.cos(angle); self.y=GROUND_Y; self.z=r*math.sin(angle)
        self.vx=self.vy=self.vz=0.0; self.alive=True; self.on_ground=True

    def to_dict(self):
        return {"id":self.id,"name":self.name,"color":self.color,"is_ai":self.is_ai,
                "glove":self.glove,"x":self.x,"y":self.y,"z":self.z,
                "vx":self.vx,"vy":self.vy,"vz":self.vz,"rot_y":self.rot_y,
                "alive":self.alive,"kills":self.kills,"session_points":self.session_points,
                "fire_trail":self.fire_trail[-4:]}

class Arena:
    def __init__(self, aid, host):
        self.id=aid; self.host=host
        self.players: Dict[str,Player]={}
        self.state="playing"; self.chat=[]
        self._task=None; self._running=True; self._last_tick=time.time()

    def add_player(self,p): self.players[p.id]=p
    def remove_player(self,pid): self.players.pop(pid,None)
    def alive_players(self): return [p for p in self.players.values() if p.alive]

    def color_for_slot(self):
        used={p.color for p in self.players.values()}
        for c in COLORS:
            if c not in used: return c
        return random.choice(COLORS)

    async def broadcast(self, msg):
        dead=[]
        for p in self.players.values():
            if p.ws and not p.is_ai:
                try: await p.ws.send_json(msg)
                except: dead.append(p.id)
        for pid in dead: self.remove_player(pid)

    async def send_state(self):
        await self.broadcast({"type":"game_state",
            "players":[p.to_dict() for p in self.players.values()],"state":self.state})

    async def send_chat(self,sender,msg,system=False):
        entry={"sender":sender,"msg":msg,"system":system}
        self.chat.append(entry)
        await self.broadcast({"type":"chat",**entry})

    async def start_loop(self):
        self._task=asyncio.create_task(self._game_loop())

    async def _game_loop(self):
        while self._running:
            now=time.time()
            dt=min(now-self._last_tick,0.1)
            self._last_tick=now
            for p in list(self.players.values()):
                if not p.alive and now>=p.respawn_at:
                    idx=list(self.players.keys()).index(p.id)
                    p.spawn(idx,len(self.players))
                    asyncio.create_task(self.send_chat("",f"🔄 {p.name} respawned!",system=True))
            for p in list(self.players.values()):
                if p.is_ai and p.alive: self._ai_tick(p,now)
            self._physics_tick(dt)
            for p in list(self.players.values()):
                if not p.alive: continue
                dist=math.sqrt(p.x**2+p.z**2)
                if p.y<FALL_THRESHOLD or dist>PLATFORM_RADIUS+3:
                    p.alive=False; p.respawn_at=now+RESPAWN_TIME
                    asyncio.create_task(self.send_chat("",f"💀 {p.name} fell off!",system=True))
            await self.send_state()
            await asyncio.sleep(TICK_RATE)

    def _physics_tick(self,dt):
        for p in self.players.values():
            if not p.alive: continue
            if not p.on_ground: p.vy+=GRAVITY*dt
            p.x+=p.vx*dt; p.y+=p.vy*dt; p.z+=p.vz*dt
            dist=math.sqrt(p.x**2+p.z**2)
            if p.y<=GROUND_Y and dist<PLATFORM_RADIUS:
                p.y=GROUND_Y; p.vy=0; p.on_ground=True
            else:
                if p.y>GROUND_Y or dist>=PLATFORM_RADIUS: p.on_ground=False
            spd_mult=2.0 if p.glove=="speed" else 1.0
            max_spd=PLAYER_SPEED*spd_mult*2
            p.vx=max(-max_spd,min(max_spd,p.vx))
            p.vz=max(-max_spd,min(max_spd,p.vz))
            friction=0.78 if p.on_ground else 0.97
            p.vx*=friction; p.vz*=friction
            now=time.time()
            p.fire_trail=[f for f in p.fire_trail if now-f["t"]<2.0]

    def _slap(self,attacker:Player,aim_angle:float,power:float=5.0):
        now=time.time()
        if now<attacker.slap_cd: return
        glove=attacker.glove
        cd=0.4 if glove=="speed" else SLAP_COOLDOWN
        face_x=math.sin(aim_angle); face_z=math.cos(aim_angle)
        hit_targets=[]

        if glove=="wave":
            for t in self.alive_players():
                if t.id==attacker.id: continue
                dx=t.x-attacker.x; dz=t.z-attacker.z
                dist=math.sqrt(dx**2+dz**2)
                if dist<SLAP_RANGE*2.2 and dist>0: hit_targets.append((t,dx/dist,dz/dist))
        elif glove=="tornado":
            for t in self.alive_players():
                if t.id==attacker.id: continue
                dx=t.x-attacker.x; dz=t.z-attacker.z
                dist=math.sqrt(dx**2+dz**2)
                if dist<SLAP_RANGE*1.8 and dist>0: hit_targets.append((t,dx/dist,dz/dist))
        elif glove=="ghost":
            nearest,nd=None,float("inf")
            for t in self.alive_players():
                if t.id==attacker.id: continue
                d=math.sqrt((t.x-attacker.x)**2+(t.z-attacker.z)**2)
                if d<nd: nearest,nd=t,d
            if nearest:
                ang=math.atan2(nearest.x-attacker.x,nearest.z-attacker.z)
                attacker.x=nearest.x-math.sin(ang)*1.5
                attacker.z=nearest.z-math.cos(ang)*1.5
                dx=nearest.x-attacker.x; dz=nearest.z-attacker.z
                dist=max(0.1,math.sqrt(dx**2+dz**2))
                hit_targets.append((nearest,dx/dist,dz/dist))
        elif glove=="magnet":
            for t in self.alive_players():
                if t.id==attacker.id: continue
                dx=t.x-attacker.x; dz=t.z-attacker.z
                dist=math.sqrt(dx**2+dz**2)
                if dist<SLAP_RANGE*2.5 and dist>0:
                    td=max(0.1,math.sqrt(t.x**2+t.z**2))
                    hit_targets.append((t,t.x/td,t.z/td))
        else:
            for t in self.alive_players():
                if t.id==attacker.id: continue
                dx=t.x-attacker.x; dz=t.z-attacker.z
                dist=math.sqrt(dx**2+dz**2)
                if dist==0 or dist>SLAP_RANGE: continue
                dot=(dx/dist)*face_x+(dz/dist)*face_z
                if dot>0.25: hit_targets.append((t,dx/dist,dz/dist))

        if not hit_targets: return
        attacker.slap_cd=now+cd

        for (target,nx,nz) in hit_targets:
            t=max(0,min(10,power))/10.0
            force=10+t*20
            if glove=="overkill": force=999
            elif glove=="bomb": force*=1.9
            elif glove=="inferno":
                force*=1.5
                attacker.fire_trail.append({"x":attacker.x,"z":attacker.z,"t":now})
            elif glove=="ghost": force*=1.7
            elif glove=="wave": force*=0.9
            elif glove=="magnet": force*=1.3
            target.vx+=nx*force; target.vz+=nz*force
            target.vy+=force*0.22; target.on_ground=False
            attacker.kills+=1; attacker.session_points+=POINTS_PER_SLAP
            if not attacker.is_ai and attacker.name in accounts:
                accounts[attacker.name]["points"]+=POINTS_PER_SLAP
                save_account(attacker.name)
            asyncio.create_task(self.send_chat("",f"👊 {attacker.name} slapped {target.name}! (+{POINTS_PER_SLAP}pts)",system=True))

    def _ai_tick(self,ai:Player,now:float):
        if now<ai.ai_next: return
        ai.ai_next=now+AI_TICK+random.uniform(0,0.25)
        alive=[p for p in self.players.values() if p.alive and p.id!=ai.id]
        if not alive: return
        def dist_to(p): return math.sqrt((p.x-ai.x)**2+(p.z-ai.z)**2)
        target=min(alive,key=dist_to)
        dx=target.x-ai.x; dz=target.z-ai.z; dist=dist_to(target)
        my_dist=math.sqrt(ai.x**2+ai.z**2)
        if my_dist>PLATFORM_RADIUS*0.82:
            ai.vx+=(-ai.x/my_dist)*PLAYER_SPEED*0.7
            ai.vz+=(-ai.z/my_dist)*PLAYER_SPEED*0.7; return
        if dist<SLAP_RANGE*1.2:
            aim=math.atan2(dx,dz); ai.rot_y=aim
            self._slap(ai,aim,random.uniform(5,10))
        elif dist>0:
            ai.vx+=(dx/dist)*PLAYER_SPEED*AI_TICK
            ai.vz+=(dz/dist)*PLAYER_SPEED*AI_TICK
            ai.rot_y=math.atan2(dx,dz)
        if ai.on_ground and random.random()<0.04:
            ai.vy=JUMP_VEL; ai.on_ground=False

arenas: Dict[str,Arena]={}

@app.get("/")
async def root(): return FileResponse("static/index.html")

@app.post("/register")
async def register(data:dict=Body(...)):
    u=str(data.get("username","")).strip()[:16]; p=str(data.get("password","")).strip()
    if not u or not p: return {"ok":False,"msg":"Fill in both fields!"}
    if len(p)<4: return {"ok":False,"msg":"Password must be 4+ chars"}
    if u in accounts: return {"ok":False,"msg":"Username taken!"}
    accounts[u]={"password":p,"points":0,"wins":0,"unlocked":["default"]}
    save_account(u)
    return {"ok":True,"username":u,"points":0,"unlocked":["default"]}

@app.post("/login")
async def login(data:dict=Body(...)):
    u=str(data.get("username","")).strip()[:16]; p=str(data.get("password","")).strip()
    if u not in accounts: return {"ok":False,"msg":"Account not found!"}
    if accounts[u]["password"]!=p: return {"ok":False,"msg":"Wrong password!"}
    d=accounts[u]
    return {"ok":True,"username":u,"points":d["points"],"wins":d["wins"],"unlocked":d["unlocked"]}

@app.post("/buy_glove")
async def buy_glove(data:dict=Body(...)):
    u=str(data.get("username","")).strip(); glove=str(data.get("glove","")).strip()
    if u not in accounts: return {"ok":False,"msg":"Not logged in!"}
    if glove not in GLOVES: return {"ok":False,"msg":"Unknown glove!"}
    if glove in accounts[u]["unlocked"]: return {"ok":False,"msg":"Already owned!"}
    cost=GLOVES[glove]["cost"]
    if accounts[u]["points"]<cost: return {"ok":False,"msg":f"Need {cost} points! You have {accounts[u]['points']}"}
    accounts[u]["points"]-=cost; accounts[u]["unlocked"].append(glove)
    save_account(u)
    return {"ok":True,"points":accounts[u]["points"],"unlocked":accounts[u]["unlocked"]}

@app.get("/gloves")
async def get_gloves(): return GLOVES

@app.get("/leaderboard")
async def leaderboard():
    s=sorted(accounts.items(),key=lambda x:x[1]["points"],reverse=True)
    return [{"name":k,"points":v["points"],"wins":v["wins"]} for k,v in s[:20]]

@app.get("/arenas")
async def list_arenas():
    return [{"id":aid,"host":a.host,"players":len(a.players),"max":LOBBY_MAX} for aid,a in arenas.items()]

@app.websocket("/ws/{arena_id}/{player_name}/{mode}")
async def ws_endpoint(ws:WebSocket,arena_id:str,player_name:str,mode:str):
    await ws.accept()
    is_bot_mode=mode.startswith("bots")
    sel_glove=mode.split(":")[-1] if ":" in mode else "default"

    if arena_id not in arenas:
        arenas[arena_id]=Arena(arena_id,player_name)
    arena=arenas[arena_id]
    if len(arena.players)>=LOBBY_MAX:
        await ws.send_json({"type":"error","msg":"Arena full!"}); await ws.close(); return

    pid=str(uuid.uuid4())[:8]; color=arena.color_for_slot()
    player=Player(pid,player_name[:16],color)
    player.ws=ws
    if player_name in accounts:
        unlocked=accounts[player_name]["unlocked"]
        player.glove=sel_glove if sel_glove in unlocked else "default"
    arena.add_player(player)
    idx=list(arena.players.keys()).index(pid)
    player.spawn(idx,len(arena.players))

    if is_bot_mode:
        for b in [p for p in arena.players.values() if p.is_ai]: arena.remove_player(b.id)
        for i in range(min(8,LOBBY_MAX-len(arena.players))):
            bname=BOT_NAMES[i%len(BOT_NAMES)]+str(random.randint(1,99))
            bpid="bot_"+str(uuid.uuid4())[:6]
            bot=Player(bpid,bname,arena.color_for_slot(),is_ai=True)
            bot.glove=random.choice(["default","speed","bomb","wave"])
            bot.spawn(len(arena.players),len(arena.players)+1)
            arena.add_player(bot)

    await ws.send_json({"type":"joined","your_id":pid})
    await arena.send_chat("",f"🖐️ {player_name} entered the arena!",system=True)
    await arena.send_state()
    if arena._task is None or arena._task.done():
        await arena.start_loop()

    try:
        while True:
            data=await ws.receive_json()
            await _handle(arena,player,data)
    except (WebSocketDisconnect,Exception):
        arena.remove_player(pid)
        await arena.send_chat("",f"👋 {player_name} left.",system=True)
        if len([p for p in arena.players.values() if not p.is_ai])==0:
            arena._running=False; arenas.pop(arena_id,None)
        else:
            await arena.send_state()

async def _handle(arena:Arena,player:Player,data:dict):
    kind=data.get("type")
    if kind=="input" and player.alive:
        inp=data.get("input",{})
        dx=float(inp.get("dx",0)); dz=float(inp.get("dz",0))
        spd=PLAYER_SPEED*(2.0 if player.glove=="speed" else 1.0)
        length=math.sqrt(dx**2+dz**2)
        if length>0:
            dx/=length; dz/=length
            player.vx+=dx*spd*TICK_RATE*10
            player.vz+=dz*spd*TICK_RATE*10
        aim=inp.get("aim_angle")
        if aim is not None: player.rot_y=float(aim)
        elif length>0: player.rot_y=math.atan2(dx,dz)
        if inp.get("jump") and player.on_ground:
            player.vy=JUMP_VEL; player.on_ground=False
        if inp.get("slap"):
            arena._slap(player,float(inp.get("aim_angle",player.rot_y)),float(inp.get("power",5.0)))
    elif kind=="chat":
        msg=str(data.get("msg",""))[:200].strip()
        if msg: await arena.send_chat(player.name,msg)
    elif kind=="change_glove":
        glove=str(data.get("glove",""))
        if player.name in accounts and glove in accounts[player.name]["unlocked"]:
            player.glove=glove; await arena.send_state()

app.mount("/static",StaticFiles(directory="static"),name="static")
