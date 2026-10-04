import discord
import aiohttp
import asyncio
import json
import base64
import re
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ─── CONFIGURAÇÃO ───────────────────────────────────────────────
DISCORD_TOKEN    = os.environ.get("DISCORD_TOKEN")
CHANNEL_ID       = 1515076780845695057
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_API_KEY")
SHEETS_JSON_FILE = os.path.join(BASE_DIR, "rise-bot-cp2.json")
SPREADSHEET_ID   = "1-JrUd_siOexwq-p92X3DCHMogPYOlu7Da6i8WSvOt6g"
MEMBERS_SHEET    = "MEMBERS"
RANKING_SHEET    = "Ranking"
DATA_DIR         = os.environ.get("DATA_DIR", BASE_DIR)  # no Railway: apontar para o Volume (ex: /data)
STATE_FILE       = os.path.join(DATA_DIR, "ranking_state.json")
ADMIN_ROLE_NAME  = "Admin"
BRASILIA_TZ      = timezone(timedelta(hours=-3))
# ────────────────────────────────────────────────────────────────

intents = discord.Intents.all()
client  = discord.Client(intents=intents)

# ── Estado persistente ───────────────────────────────────────────

def load_state():
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    # Primeira execucao no Railway: importa o estado do PC se a variavel INITIAL_STATE_JSON existir
    initial = os.environ.get("INITIAL_STATE_JSON")
    if initial:
        try:
            state = json.loads(initial)
            save_state(state)
            return state
        except json.JSONDecodeError:
            print("[AVISO] INITIAL_STATE_JSON invalido, iniciando com estado vazio.")
    return {
        "player_points": {},        # {"PirataDM": 8, ...} — acumula pra sempre
        "processed_messages": [],
        "last_message_id": None,
        "excluded_players": []      # nomes (normalizados) removidos manualmente do ranking
    }

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ── Google Sheets Auth ───────────────────────────────────────────

async def get_sheets_token() -> str:
    """Gera token de acesso OAuth2 usando Service Account."""
    import time
    creds_env = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_env:
        creds = json.loads(creds_env)
    else:
        with open(SHEETS_JSON_FILE, "r") as f:
            creds = json.load(f)

    now = int(time.time())
    payload = {
        "iss": creds["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600
    }

    import base64 as b64
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    header  = b64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
    body    = b64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
    message = header + b"." + body

    private_key = serialization.load_pem_private_key(
        creds["private_key"].encode(), password=None, backend=default_backend()
    )
    signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    sig_b64   = b64.urlsafe_b64encode(signature).rstrip(b"=")
    jwt_token = (message + b"." + sig_b64).decode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://oauth2.googleapis.com/token",
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt_token}
        ) as resp:
            result = await resp.json()
            return result["access_token"]

async def sheets_get(token: str, range_: str) -> list:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
            result = await resp.json()
            return result.get("values", [])

async def sheets_update(token: str, range_: str, values: list):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_}?valueInputOption=RAW"
    async with aiohttp.ClientSession() as session:
        async with session.put(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"range": range_, "majorDimension": "ROWS", "values": values}
        ) as resp:
            return await resp.json()

# ── Carregar membros da planilha ────────────────────────────────

async def load_members() -> dict:
    """Retorna {nome_normalizado: nome_oficial} lendo a aba MEMBERS (coluna A = Nome, coluna B = CP)."""
    token  = await get_sheets_token()
    values = await sheets_get(token, f"'{MEMBERS_SHEET}'!A2:B1000")
    members = {}
    for row in values:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
        members[normalize(name)] = name
    return members

def normalize(name: str) -> str:
    """Normaliza nome para comparação insensível a case e ambiguidades l/I/1."""
    n = name.lower().strip()
    n = n.replace("1", "l").replace("i", "l")
    return n

def parse_cmd_name_qty(content: str, prefix: str):
    """Extrai nome e quantidade de um comando como '!addpontos PirataDM 5'."""
    rest = content.strip()[len(prefix):].strip()
    if not rest:
        return None, None
    parts = rest.rsplit(maxsplit=1)
    if len(parts) != 2:
        return None, None
    name, qty_str = parts
    try:
        return name.strip(), int(qty_str)
    except ValueError:
        return None, None

def is_weekend_window(dt: datetime) -> bool:
    """Sexta 20h00 até domingo 23h59, horário de Brasília."""
    weekday = dt.weekday()  # Segunda=0 ... Domingo=6
    if weekday == 4:
        return dt.hour >= 20
    if weekday in (5, 6):
        return True
    return False

def credit_points(state: dict, name: str, qtd: int):
    """Soma pontos reaproveitando a chave já existente (case-insensitive), evitando duplicar o mesmo jogador com grafias diferentes."""
    norm = normalize(name)
    existing = next((k for k in state["player_points"] if normalize(k) == norm), None)
    if existing:
        state["player_points"][existing] += qtd
        return existing, state["player_points"][existing]
    state["player_points"][name] = qtd
    return name, qtd

# ── Extrair nomes via Claude Vision ─────────────────────────────

async def extract_names_from_image(image_bytes: bytes, members: dict) -> list:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    roster = list(members.values())

    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 1000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": (
                    "CRITICAL: Your entire response must be ONLY a raw JSON array of strings. "
                    "No explanations, no markdown, no text before or after. Just the array.\n\n"
                    "This is a Lineage 2 screenshot. Extract player character names ONLY from "
                    "the Party Members panel and the Leader field above it. "
                    "Do NOT extract names from chat, the game world, clan titles, or system messages.\n\n"
                    "Official member roster (use for OCR correction of ambiguous characters l/I/1/L):\n"
                    f"{json.dumps(roster)}\n\n"
                    "If a name you read closely matches one in the roster (ignoring case and l/I/1/L swaps), "
                    "return it EXACTLY as it appears in the roster.\n\n"
                    "REMEMBER: respond with ONLY the JSON array. "
                    'Example: ["PirataDM", "HanzelL"]'
                )}
            ]
        }]
    }

    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.anthropic.com/v1/messages", json=payload, headers=headers) as resp:
            result = await resp.json()
            if resp.status != 200:
                raise Exception(f"API erro {resp.status}: {result.get('error', {}).get('message', str(result))}")
            text = result["content"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
            try:
                names = json.loads(text)
            except json.JSONDecodeError:
                match = re.search(r'\[[\s\S]*?\]', text)
                if match:
                    names = json.loads(match.group(0))
                else:
                    raise Exception(f"Resposta inválida: {text[:200]}")
            return [n.strip() for n in names if isinstance(n, str) and n.strip()]

# ── Atualizar Google Sheets ──────────────────────────────────────

async def update_sheets(state: dict, members: dict):
    token = await get_sheets_token()
    excluded = set(state.get("excluded_players", []))
    raw_pts = {name: pts for name, pts in state.get("player_points", {}).items() if normalize(name) not in excluded}

    # Rede de segurança: mescla qualquer duplicata remanescente (mesmo nome com grafia diferente),
    # somando os pontos e preferindo a grafia oficial cadastrada na aba MEMBERS.
    player_pts = {}
    for name, pts in raw_pts.items():
        norm = normalize(name)
        canonical = members.get(norm, name)  # usa a grafia oficial da planilha MEMBERS, se existir
        existing_key = next((k for k in player_pts if normalize(k) == norm), None)
        if existing_key:
            player_pts[existing_key] += pts
        else:
            player_pts[canonical] = pts

    # Jogadores com 0 pontos (ou menos) não aparecem na planilha
    player_pts = {name: pts for name, pts in player_pts.items() if pts > 0}

    rows = [["Nome", "Pontos"]]
    for name, pts in sorted(player_pts.items(), key=lambda x: (-x[1], x[0])):
        rows.append([name, pts])

    clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/'{RANKING_SHEET}'!A1:B1000:clear"
    async with aiohttp.ClientSession() as session:
        await session.post(clear_url, headers={"Authorization": f"Bearer {token}"})

    await sheets_update(token, f"'{RANKING_SHEET}'!A1:B{len(rows)}", rows)

    # Também mescla o estado local, pra parar de arrastar a duplicata pra sempre
    state["player_points"] = player_pts


# ── Processar mensagens novas ────────────────────────────────────

async def process_new_messages(channel, state: dict, members: dict, ignorar_cursor: bool = False) -> list:
    """Processa imagens novas e retorna lista de eventos processados."""
    after_id = None if ignorar_cursor else state.get("last_message_id")
    kwargs = {"limit": None, "oldest_first": True}
    if after_id:
        kwargs["after"] = discord.Object(id=int(after_id))

    events = []
    ultimo_id = after_id

    async for msg in channel.history(**kwargs):
        if msg.author.bot:
            ultimo_id = str(msg.id)
            continue

        msg_id = str(msg.id)
        images = [a for a in msg.attachments if a.content_type and "image" in a.content_type]
        if not images or msg_id in state["processed_messages"]:
            ultimo_id = msg_id
            continue

        msg_dt = msg.created_at.replace(tzinfo=timezone.utc).astimezone(BRASILIA_TZ)
        pts_por_presenca = 2 if is_weekend_window(msg_dt) else 1

        event_names_validos = []
        event_names_ignorados = []
        ja_pontuados_no_evento = set()  # evita contar 2x o mesmo jogador (ex: aparece como Leader E como Party Member)

        for attachment in images:
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    image_bytes = await resp.read()
            try:
                names = await extract_names_from_image(image_bytes, members)
                excluded = set(state.get("excluded_players", []))
                for name in names:
                    norm = normalize(name)
                    if norm not in members or norm in excluded:
                        event_names_ignorados.append(name)
                        continue
                    if norm in ja_pontuados_no_evento:
                        continue  # já pontuou nesse mesmo evento, ignora repetição
                    official = members[norm]
                    event_names_validos.append(official)
                    credit_points(state, official, pts_por_presenca)
                    ja_pontuados_no_evento.add(norm)
            except Exception as e:
                print(f"[ERRO] {attachment.filename}: {e}")

        events.append({
            "msg_id": msg_id,
            "date": msg_dt.strftime("%Y-%m-%d %H:%M"),
            "is_weekend": pts_por_presenca == 2,
            "pontos_por_presenca": pts_por_presenca,
            "validos": event_names_validos,
            "ignorados": event_names_ignorados
        })

        state["processed_messages"].append(msg_id)
        ultimo_id = msg_id

    if ultimo_id:
        state["last_message_id"] = ultimo_id

    return events

# ── Formatar resumo do !atualizar ────────────────────────────────

def format_update_summary(events: list, state: dict) -> str:
    if not events:
        return "✅ Nenhum evento novo encontrado."

    lines = [f"📊 **{len(events)} evento(s) processado(s)**\n"]
    for i, ev in enumerate(events, 1):
        weekend_str = f" 🌟 **{ev['pontos_por_presenca']} pts/presença**" if ev["is_weekend"] else f" ({ev['pontos_por_presenca']} pt/presença)"
        lines.append(f"**Evento {i}** — {ev['date']}{weekend_str}")
        if ev["validos"]:
            lines.append(f"  ✅ Pontuaram: {', '.join(ev['validos'])}")
        if ev["ignorados"]:
            lines.append(f"  ⚠️ Não cadastrados (ignorados): {', '.join(ev['ignorados'])}")
        lines.append("")

    lines.append("```")
    lines.append("🏆 RANKING")
    lines.append("─" * 30)
    sorted_players = sorted(state["player_points"].items(), key=lambda x: -x[1])
    for i, (name, pts) in enumerate(sorted_players[:30], 1):
        lines.append(f"{i:>2}. {name:<20} {pts:>4} pts")
    lines.append("─" * 30)
    lines.append("```")
    lines.append("✅ Google Sheets atualizado!")

    return "\n".join(lines)

# ── Eventos Discord ──────────────────────────────────────────────

def is_admin(message: discord.Message) -> bool:
    return any(role.name == ADMIN_ROLE_NAME for role in message.author.roles)

async def send_long_message(channel, text: str, limite: int = 1900):
    """Envia texto longo em vários blocos, respeitando o limite de 2000 caracteres do Discord."""
    if len(text) <= limite:
        await channel.send(text)
        return

    linhas = text.split("\n")
    bloco = ""
    for linha in linhas:
        # Se uma linha sozinha já for maior que o limite, corta ela também
        while len(linha) > limite:
            await channel.send(linha[:limite])
            linha = linha[limite:]

        if len(bloco) + len(linha) + 1 > limite:
            await channel.send(bloco)
            bloco = linha
        else:
            bloco = f"{bloco}\n{linha}" if bloco else linha

    if bloco:
        await channel.send(bloco)

@client.event
async def on_message(message: discord.Message):
    if message.channel.id != CHANNEL_ID:
        return
    if message.author.bot:
        return

    cmd = message.content.strip().lower()
    admin_commands = ("!atualizar", "!forcar", "!resetpontos")
    admin_prefixes = ("!addpontos ", "!removepontos ", "!removeplayer ", "!addplayer ")

    if cmd in admin_commands or cmd.startswith(admin_prefixes):
        if not is_admin(message):
            await message.channel.send(f"⛔ Apenas membros com o cargo **{ADMIN_ROLE_NAME}** podem usar este comando.")
            return

    # !atualizar — processa imagens novas e atualiza Sheets
    if cmd == "!atualizar":
        await message.channel.send("⏳ Processando eventos novos...")
        try:
            members = await load_members()
            if not members:
                await message.channel.send("❌ Não foi possível carregar a aba MEMBERS. Verifique o compartilhamento da planilha.")
                return

            state = load_state()
            events = await process_new_messages(message.channel, state, members)
            await update_sheets(state, members)
            save_state(state)

            summary = format_update_summary(events, state)
            await send_long_message(message.channel, summary)
        except Exception as e:
            await message.channel.send(f"❌ Erro durante atualização: {e}")
        return

    # !forcar — revarre todo o canal sem resetar dados, só pula duplicados
    if cmd == "!forcar":
        await message.channel.send("⏳ Revarrendo todo o canal sem resetar dados...")
        try:
            members = await load_members()
            if not members:
                await message.channel.send("❌ Não foi possível carregar a aba MEMBERS.")
                return
            state = load_state()
            events = await process_new_messages(message.channel, state, members, ignorar_cursor=True)
            await update_sheets(state, members)
            save_state(state)
            summary = format_update_summary(events, state)
            await send_long_message(message.channel, summary)
        except Exception as e:
            await message.channel.send(f"❌ Erro: {e}")
        return

    # !ranking — mostra ranking atual sem processar nada (qualquer um pode usar)
    if cmd == "!ranking":
        state = load_state()
        player_pts = {name: pts for name, pts in state.get("player_points", {}).items() if pts > 0}
        if not player_pts:
            await message.channel.send("Nenhuma pontuação registrada ainda.")
            return
        lines = ["```", "🏆 RANKING", "─" * 30]
        for i, (name, pts) in enumerate(sorted(player_pts.items(), key=lambda x: -x[1])[:30], 1):
            lines.append(f"{i:>2}. {name:<20} {pts:>4} pts")
        lines.append("─" * 30)
        lines.append("```")
        await send_long_message(message.channel, "\n".join(lines))
        return

    # !addpontos NomeDoJogador quantidade — adiciona pontos manualmente (admin)
    if cmd.startswith("!addpontos "):
        name, qtd = parse_cmd_name_qty(message.content, "!addpontos")
        if name is None:
            await message.channel.send("❌ Uso correto: `!addpontos NomeDoJogador quantidade`")
            return
        state = load_state()
        norm = normalize(name)
        state["excluded_players"] = [n for n in state.get("excluded_players", []) if n != norm]
        existing = next((k for k in state["player_points"] if normalize(k) == norm), None)
        if existing:
            state["player_points"][existing] += qtd
            novo_total = state["player_points"][existing]
        else:
            state["player_points"][name] = qtd
            novo_total = qtd
        members = await load_members()
        await update_sheets(state, members)
        save_state(state)
        await message.channel.send(f"✅ **+{qtd} pts** para **{name}** (novo total: **{novo_total} pts**) — Sheets atualizado!")
        return

    # !removepontos NomeDoJogador quantidade — remove pontos manualmente (admin)
    if cmd.startswith("!removepontos "):
        name, qtd = parse_cmd_name_qty(message.content, "!removepontos")
        if name is None:
            await message.channel.send("❌ Uso correto: `!removepontos NomeDoJogador quantidade`")
            return
        state = load_state()
        norm = normalize(name)
        existing = next((k for k in state["player_points"] if normalize(k) == norm), None)
        if not existing:
            await message.channel.send(f"❌ Jogador **{name}** não encontrado no ranking.")
            return
        state["player_points"][existing] -= qtd
        novo_total = state["player_points"][existing]
        members = await load_members()
        await update_sheets(state, members)
        save_state(state)
        await message.channel.send(f"✅ **-{qtd} pts** de **{existing}** (novo total: **{novo_total} pts**) — Sheets atualizado!")
        return

    # !removeplayer NomeDoJogador — remove um jogador completamente do ranking (admin)
    if cmd.startswith("!removeplayer "):
        name = message.content.strip()[len("!removeplayer "):].strip()
        if not name:
            await message.channel.send("❌ Uso correto: `!removeplayer NomeDoJogador`")
            return
        state = load_state()
        norm = normalize(name)
        if "excluded_players" not in state:
            state["excluded_players"] = []
        if norm not in state["excluded_players"]:
            state["excluded_players"].append(norm)
        # remove também o registro de pontos, se existir
        existing = next((k for k in state["player_points"] if normalize(k) == norm), None)
        if existing:
            del state["player_points"][existing]
        members = await load_members()
        await update_sheets(state, members)
        save_state(state)
        await message.channel.send(f"✅ **{name}** removido do ranking (não aparece mais na planilha e não pontuará em novos eventos até ser readicionado com `!addplayer`).")
        return

    # !addplayer NomeDoJogador — readiciona um jogador removido anteriormente (admin)
    if cmd.startswith("!addplayer "):
        name = message.content.strip()[len("!addplayer "):].strip()
        if not name:
            await message.channel.send("❌ Uso correto: `!addplayer NomeDoJogador`")
            return
        state = load_state()
        norm = normalize(name)
        state["excluded_players"] = [n for n in state.get("excluded_players", []) if n != norm]
        members = await load_members()
        await update_sheets(state, members)
        save_state(state)
        await message.channel.send(f"✅ **{name}** readicionado ao ranking (volta a pontuar normalmente e a aparecer na planilha).")
        return

    # !resetpontos — zera toda a pontuação (admin)
    if cmd == "!resetpontos":
        state = load_state()
        state["player_points"] = {}
        members = await load_members()
        await update_sheets(state, members)
        save_state(state)
        await message.channel.send("✅ Pontuação de todos os jogadores zerada — Sheets atualizado!")
        return

@client.event
async def on_ready():
    print(f"✅ Bot conectado como {client.user}")
    print(f"📡 Canal: {CHANNEL_ID}")
    print(f"📊 Planilha: {SPREADSHEET_ID}")
    print(f"💡 Comandos: !atualizar | !forcar | !ranking | !addpontos | !removepontos | !removeplayer | !addplayer | !resetpontos")

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Defina a variável de ambiente DISCORD_TOKEN (arquivo .env).")
    if not ANTHROPIC_KEY:
        raise RuntimeError("Defina a variável de ambiente ANTHROPIC_API_KEY (arquivo .env).")
    client.run(DISCORD_TOKEN)
