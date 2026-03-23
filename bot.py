import os
import io
import re
import json
import shlex
import sqlite3
from decimal import Decimal, InvalidOperation
from contextlib import closing

import discord
import qrcode
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIG
# =========================
IMAGEM_PADRAO = "https://i.imgur.com/aUCWSvM.png"

CANAL_PARTIDAS_PENDENTES_ID = 1484537851147190404
CANAL_LOGS_STAFF_ID = 1484549436704166020
CANAL_ANALISES_PENDENTES_ID = 1485739705617223793

NOME_CARGO_ADM = "ADM"
NOME_CARGO_SS = "SS"

NOME_CATEGORIA_EM_ANDAMENTO = "EM ANDAMENTO"
NOME_CATEGORIA_FINALIZADAS = "FINALIZADAS"
NOME_CATEGORIA_CONTROLE = "CONTROLES ADM"

PIX_CIDADE_PADRAO = "TAVARES"

DB_FILE = "bot_data.sqlite3"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# =========================
# DB
# =========================
def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column_exists(table_name: str, column_name: str, column_definition: str):
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table_name})")
        cols = [row["name"] for row in cur.fetchall()]
        if column_name not in cols:
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
            conn.commit()


def db_init():
    with closing(db_connect()) as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS panels (
            panel_id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            mode TEXT NOT NULL,
            info TEXT NOT NULL,
            image_url TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS panel_players (
            panel_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            selected_option TEXT NOT NULL,
            PRIMARY KEY(panel_id, user_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            match_id INTEGER PRIMARY KEY,
            panel_id TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            mode TEXT NOT NULL,
            info TEXT NOT NULL,
            status TEXT NOT NULL,
            players_details_json TEXT NOT NULL,
            confirmed_players_json TEXT NOT NULL,
            private_channel_id INTEGER,
            control_channel_id INTEGER,
            pending_message_id INTEGER,
            confirmation_message_id INTEGER,
            control_message_id INTEGER,
            finish_message_id INTEGER,
            claimed_by INTEGER,
            charge_amount TEXT,
            pix_receiver_name TEXT,
            pix_key TEXT,
            room_id TEXT,
            room_password TEXT,
            analysis_requested INTEGER DEFAULT 0,
            analysis_message_id INTEGER,
            analysis_claimed_by INTEGER
        )
        """)

        cur.execute("SELECT value FROM meta WHERE key = 'counter'")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO meta (key, value) VALUES ('counter', '0')")

        conn.commit()

    ensure_column_exists("matches", "control_channel_id", "INTEGER")
    ensure_column_exists("matches", "control_message_id", "INTEGER")
    ensure_column_exists("matches", "finish_message_id", "INTEGER")
    ensure_column_exists("matches", "claimed_by", "INTEGER")
    ensure_column_exists("matches", "charge_amount", "TEXT")
    ensure_column_exists("matches", "pix_receiver_name", "TEXT")
    ensure_column_exists("matches", "pix_key", "TEXT")
    ensure_column_exists("matches", "room_id", "TEXT")
    ensure_column_exists("matches", "room_password", "TEXT")
    ensure_column_exists("matches", "analysis_requested", "INTEGER DEFAULT 0")
    ensure_column_exists("matches", "analysis_message_id", "INTEGER")
    ensure_column_exists("matches", "analysis_claimed_by", "INTEGER")


def get_counter() -> int:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM meta WHERE key = 'counter'")
        return int(cur.fetchone()["value"])


def next_match_id() -> int:
    current = get_counter()
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE meta SET value = ? WHERE key = 'counter'", (str(current + 1),))
        conn.commit()
    return current


# =========================
# HELPERS
# =========================
def fmt_match_id(match_id: int) -> str:
    return str(match_id).zfill(6)


def find_category(guild: discord.Guild, category_name: str):
    return discord.utils.get(guild.categories, name=category_name)


def panel_row(panel_id: str):
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM panels WHERE panel_id = ?", (panel_id,))
        return cur.fetchone()


def panel_players(panel_id: str) -> list[dict]:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, selected_option
            FROM panel_players
            WHERE panel_id = ?
            ORDER BY rowid ASC
        """, (panel_id,))
        return [{"user_id": r["user_id"], "selected_option": r["selected_option"]} for r in cur.fetchall()]


def match_row(match_id: int):
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
        return cur.fetchone()


def is_adm_member(member: discord.Member) -> bool:
    return any(role.name.lower() == NOME_CARGO_ADM.lower() for role in member.roles)


def is_ss_member(member: discord.Member) -> bool:
    return any(role.name.lower() == NOME_CARGO_SS.lower() for role in member.roles)


def can_manage_match(member: discord.Member, row) -> bool:
    if member.guild.owner_id == member.id:
        return True
    if not is_adm_member(member):
        return False
    return row["claimed_by"] is not None and int(row["claimed_by"]) == member.id


def parse_decimal_brl(value_str: str) -> Decimal:
    value_str = value_str.strip().replace("R$", "").replace(" ", "")
    value_str = value_str.replace(".", "").replace(",", ".")
    value = Decimal(value_str)
    if value <= 0:
        raise InvalidOperation
    return value.quantize(Decimal("0.01"))


def format_brl(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'))}".replace(".", ",")


def format_money_for_channel(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"))).replace(".", "-")


def safe_channel_name(text: str) -> str:
    text = text.lower().strip().replace(" ", "-")
    text = re.sub(r"[^a-z0-9,\-]", "", text)
    text = re.sub(r"-{2,}", "-", text)
    return text[:95] if text else "canal"


def normalize_pix_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-zÀ-ÿ0-9 ]+", "", name).strip().upper()
    return cleaned[:25] if cleaned else "RECEBEDOR"


def normalize_pix_city(city: str) -> str:
    cleaned = re.sub(r"[^A-Za-zÀ-ÿ0-9 ]+", "", city).strip().upper()
    return cleaned[:15] if cleaned else "BRASIL"


def emv(field_id: str, value: str) -> str:
    return f"{field_id}{len(value):02d}{value}"


def crc16_ccitt(payload: str) -> str:
    polynomial = 0x1021
    crc = 0xFFFF

    for ch in payload:
        crc ^= (ord(ch) << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ polynomial) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    return f"{crc:04X}"


def build_pix_payload(pix_key: str, receiver_name: str, amount: Decimal, city: str = PIX_CIDADE_PADRAO) -> str:
    pix_key = pix_key.strip()
    receiver_name = normalize_pix_name(receiver_name)
    city = normalize_pix_city(city)
    amount_str = f"{amount:.2f}"

    merchant_account_info = (
        emv("00", "BR.GOV.BCB.PIX") +
        emv("01", pix_key)
    )

    additional_data = emv("05", "***")

    payload = (
        emv("00", "01") +
        emv("26", merchant_account_info) +
        emv("52", "0000") +
        emv("53", "986") +
        emv("54", amount_str) +
        emv("58", "BR") +
        emv("59", receiver_name) +
        emv("60", city) +
        emv("62", additional_data) +
        "6304"
    )

    crc = crc16_ccitt(payload)
    return payload + crc


def generate_qr_file(payload: str, filename: str = "pix_qrcode.png") -> discord.File:
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return discord.File(buffer, filename=filename)


def get_match_players_details(row) -> list[dict]:
    try:
        return json.loads(row["players_details_json"])
    except Exception:
        return []


def build_players_display(details: list[dict]) -> str:
    if not details:
        return "Nenhum jogador"
    lines = []
    for item in details:
        uid = item["user_id"]
        selected = item.get("selected_option", "").strip()
        if selected:
            lines.append(f"<@{uid}> — {selected}")
        else:
            lines.append(f"<@{uid}>")
    return "\n".join(lines)


def build_players_vs(details: list[dict]) -> str:
    if not details:
        return "Nenhum jogador"
    parts = []
    for item in details:
        uid = item["user_id"]
        selected = item.get("selected_option", "").strip()
        if selected:
            parts.append(f"<@{uid}> ({selected})")
        else:
            parts.append(f"<@{uid}>")
    return " vs ".join(parts)


def panel_option_labels(mode: str) -> list[str]:
    mode_clean = mode.lower()

    if "mista" in mode_clean:
        return ["1 emulador", "2 emuladores", "3 emuladores"]

    if "mobile" in mode_clean or "móbile" in mode_clean:
        return ["Gelo normal", "Gelo infinito"]

    if "emulador" in mode_clean:
        return ["Gelo normal", "Gelo infinito"]

    return ["Entrar na fila"]


async def send_staff_log(guild: discord.Guild, text: str):
    channel = guild.get_channel(CANAL_LOGS_STAFF_ID)
    if isinstance(channel, discord.TextChannel):
        await channel.send(text)


# =========================
# EMBEDS
# =========================
def build_panel_embed(panel_id: str) -> discord.Embed:
    panel = panel_row(panel_id)
    players = panel_players(panel_id)
    players_text = build_players_display(players) if players else "Nenhum jogador na fila"

    embed = discord.Embed(title=panel["title"], color=0x2ECC71)
    embed.description = (
        f"🎮 **Modo:**\n{panel['mode']}\n\n"
        f"💸 **Valor:**\n{panel['info']}\n\n"
        f"👤 **Líderes na fila:**\n{players_text}"
    )
    embed.set_thumbnail(url=panel["image_url"])
    return embed


def build_confirmation_embed(match_id: int) -> discord.Embed:
    row = match_row(match_id)
    details = get_match_players_details(row)
    confirmed = json.loads(row["confirmed_players_json"])

    lines = []
    for item in details:
        uid = item["user_id"]
        option = item.get("selected_option", "")
        if uid in confirmed:
            lines.append(f"✅ <@{uid}> ({option}) confirmou")
        else:
            lines.append(f"⏳ <@{uid}> ({option}) aguardando")

    embed = discord.Embed(
        title=f"Partida #{fmt_match_id(match_id)}",
        color=0xF1C40F,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"💸 **Valor:** {row['info']}\n"
            f"👤 **Líderes:**\n{build_players_display(details)}\n\n"
            f"📌 **Status:** Aguardando confirmação\n\n"
            + "\n".join(lines)
        )
    )
    return embed


def build_pending_match_embed(match_id: int) -> discord.Embed:
    row = match_row(match_id)
    details = get_match_players_details(row)

    embed = discord.Embed(
        title=f"Partida #{fmt_match_id(match_id)}",
        color=0x3498DB,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"💸 **Valor:** {row['info']}\n"
            f"👤 **Líderes:**\n{build_players_display(details)}\n\n"
            f"📌 **Status:** 🟡 Aguardando ADM"
        )
    )
    return embed


def build_status_embed(match_id: int, status_label: str, color: int) -> discord.Embed:
    row = match_row(match_id)
    details = get_match_players_details(row)

    adm_line = ""
    if row["claimed_by"]:
        adm_line = f"\n✅ **ADM responsável:** <@{row['claimed_by']}>"

    embed = discord.Embed(
        title=f"Partida #{fmt_match_id(match_id)}",
        color=color,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"💸 **Valor:** {row['info']}\n"
            f"👤 **Líderes:**\n{build_players_display(details)}"
            f"{adm_line}\n\n"
            f"📌 **Status:** {status_label}"
        )
    )
    return embed


def build_analysis_embed(match_id: int) -> discord.Embed:
    row = match_row(match_id)
    details = get_match_players_details(row)

    return discord.Embed(
        title=f"Análise da Partida #{fmt_match_id(match_id)}",
        color=0x9B59B6,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"💸 **Valor:** {row['info']}\n"
            f"👤 **Líderes:**\n{build_players_display(details)}\n\n"
            f"📌 **Status:** Análise solicitada"
        )
    )


# =========================
# UPDATE MESSAGES
# =========================
async def refresh_panel_message(panel_id: str):
    panel = panel_row(panel_id)
    if not panel:
        return

    guild = bot.get_guild(panel["guild_id"])
    if guild is None:
        return

    channel = guild.get_channel(panel["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(int(panel_id))
        view = PanelQueueView(panel_id)
        await msg.edit(embed=build_panel_embed(panel_id), view=view)
    except discord.NotFound:
        pass


async def refresh_confirmation_message(guild: discord.Guild, match_id: int):
    row = match_row(match_id)
    if not row or not row["confirmation_message_id"]:
        return

    channel = guild.get_channel(row["private_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["confirmation_message_id"])
        await msg.edit(embed=build_confirmation_embed(match_id), view=PlayerConfirmationView(match_id))
    except discord.NotFound:
        pass


async def update_confirmation_message_status(guild: discord.Guild, match_id: int, status_text: str, color: int):
    row = match_row(match_id)
    if not row or not row["confirmation_message_id"]:
        return

    channel = guild.get_channel(row["private_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["confirmation_message_id"])
        embed = build_status_embed(match_id, status_text, color)
        await msg.edit(embed=embed, view=None)
    except discord.NotFound:
        pass


async def update_pending_message(guild: discord.Guild, match_id: int, status_text: str, color: int, keep_claim_button: bool = False):
    row = match_row(match_id)
    if not row or not row["pending_message_id"]:
        return

    pending_channel = guild.get_channel(CANAL_PARTIDAS_PENDENTES_ID)
    if not isinstance(pending_channel, discord.TextChannel):
        return

    try:
        msg = await pending_channel.fetch_message(row["pending_message_id"])
        embed = build_status_embed(match_id, status_text, color)
        view = ClaimMatchView(match_id) if keep_claim_button else None
        await msg.edit(embed=embed, view=view)
    except discord.NotFound:
        pass


async def update_control_message(guild: discord.Guild, match_id: int):
    row = match_row(match_id)
    if not row or not row["control_message_id"] or not row["control_channel_id"]:
        return

    channel = guild.get_channel(row["control_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["control_message_id"])
    except discord.NotFound:
        return

    details = get_match_players_details(row)

    status_map = {
        "claimed": "Em preparação",
        "in_progress": "Em andamento",
        "payment_pending": "Aguardando confirmação de pagamento",
        "payment_confirmed": "Pagamento confirmado",
        "room_sent": "Sala enviada",
        "finished": "Finalizada"
    }

    base_text = (
        f"🎮 **Partida #{fmt_match_id(match_id)}**\n"
        f"👤 **Líderes:** {build_players_vs(details)}\n"
        f"📌 **Status:** {status_map.get(row['status'], row['status'])}\n\n"
    )

    if row["status"] == "room_sent":
        base_text += "Use as opções abaixo para chamar SS ou registrar W.O."

    view = ControlMatchView(match_id) if row["status"] in (
        "claimed", "in_progress", "payment_pending", "payment_confirmed", "room_sent"
    ) else None

    await msg.edit(content=base_text, view=view)


# =========================
# FINALIZE / WO HELPERS
# =========================
async def finalize_match(guild: discord.Guild, match_id: int, actor_mention: str, reason_text: str | None = None):
    row = match_row(match_id)
    if not row:
        return

    private_channel = guild.get_channel(row["private_channel_id"])
    control_channel = guild.get_channel(row["control_channel_id"])

    if not isinstance(private_channel, discord.TextChannel):
        return

    final_cat = find_category(guild, NOME_CATEGORIA_FINALIZADAS)

    if final_cat:
        await private_channel.edit(
            name=f"finalizados-{fmt_match_id(match_id)}",
            category=final_cat
        )
    else:
        await private_channel.edit(
            name=f"finalizados-{fmt_match_id(match_id)}"
        )

    details = get_match_players_details(row)

    for item in details:
        member = guild.get_member(int(item["user_id"]))
        if member:
            await private_channel.set_permissions(member, overwrite=None)

    if row["claimed_by"]:
        adm_member = guild.get_member(int(row["claimed_by"]))
        if adm_member:
            await private_channel.set_permissions(adm_member, overwrite=None)

    if row["analysis_claimed_by"]:
        ss_member = guild.get_member(int(row["analysis_claimed_by"]))
        if ss_member:
            await private_channel.set_permissions(ss_member, overwrite=None)

    owner = guild.owner
    if owner:
        await private_channel.set_permissions(
            owner,
            view_channel=True,
            send_messages=True,
            read_message_history=True
        )

    await private_channel.set_permissions(
        guild.default_role,
        view_channel=False
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE matches SET status = 'finished' WHERE match_id = ?",
            (match_id,)
        )
        conn.commit()

    await update_pending_message(guild, match_id, "✅ Finalizada", 0x95A5A6)
    await update_confirmation_message_status(guild, match_id, "✅ Finalizada", 0x95A5A6)

    log_text = f"✅ Partida #{fmt_match_id(match_id)} finalizada por {actor_mention}"
    if reason_text:
        log_text += f" | {reason_text}"
    await send_staff_log(guild, log_text)

    if isinstance(control_channel, discord.TextChannel):
        await control_channel.delete(reason="Partida finalizada")


# =========================
# VIEWS
# =========================
class PanelQueueView(discord.ui.View):
    def __init__(self, panel_id: str):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        panel = panel_row(panel_id)
        options = panel_option_labels(panel["mode"]) if panel else ["Entrar na fila"]

        for index, label in enumerate(options):
            btn = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                custom_id=f"panel_join_{panel_id}_{index}"
            )
            btn.callback = self.make_join_callback(label)
            self.add_item(btn)

        leave_btn = discord.ui.Button(
            label="Sair da fila",
            style=discord.ButtonStyle.danger,
            emoji="🔻",
            custom_id=f"panel_leave_{panel_id}"
        )
        leave_btn.callback = self.leave_callback
        self.add_item(leave_btn)

    def make_join_callback(self, selected_label: str):
        async def callback(interaction: discord.Interaction):
            panel = panel_row(self.panel_id)
            if not panel:
                await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
                return

            uid = str(interaction.user.id)
            players = panel_players(self.panel_id)

            if any(p["user_id"] == uid for p in players):
                await interaction.response.send_message("Você já está nessa fila.", ephemeral=True)
                return

            with closing(db_connect()) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO panel_players (panel_id, user_id, selected_option) VALUES (?, ?, ?)",
                    (self.panel_id, uid, selected_label)
                )
                conn.commit()

            await interaction.response.defer()
            await refresh_panel_message(self.panel_id)
            await try_create_match_from_queue(interaction.guild, self.panel_id)

        return callback

    async def leave_callback(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        players = panel_players(self.panel_id)

        if not any(p["user_id"] == uid for p in players):
            await interaction.response.send_message("Você não está nessa fila.", ephemeral=True)
            return

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM panel_players WHERE panel_id = ? AND user_id = ?", (self.panel_id, uid))
            conn.commit()

        await interaction.response.defer()
        await refresh_panel_message(self.panel_id)


class PlayerConfirmationView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

        confirm_btn = discord.ui.Button(
            label="Confirmar",
            style=discord.ButtonStyle.success,
            custom_id=f"confirm_match_{match_id}"
        )
        cancel_btn = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.danger,
            custom_id=f"cancel_match_{match_id}"
        )

        confirm_btn.callback = self.confirm_callback
        cancel_btn.callback = self.cancel_callback

        self.add_item(confirm_btn)
        self.add_item(cancel_btn)

    async def confirm_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if row["status"] != "awaiting_confirmation":
            await interaction.response.send_message("Essa partida não está mais aguardando confirmação.", ephemeral=True)
            return

        uid = str(interaction.user.id)
        details = get_match_players_details(row)
        player_ids = [p["user_id"] for p in details]

        if uid not in player_ids:
            await interaction.response.send_message("Você não faz parte dessa partida.", ephemeral=True)
            return

        confirmed = json.loads(row["confirmed_players_json"])
        if uid in confirmed:
            await interaction.response.send_message("Você já confirmou.", ephemeral=True)
            return

        confirmed.append(uid)

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE matches SET confirmed_players_json = ? WHERE match_id = ?",
                (json.dumps(confirmed), self.match_id)
            )
            conn.commit()

        await interaction.response.defer()
        await refresh_confirmation_message(interaction.guild, self.match_id)

        if len(confirmed) == len(player_ids):
            await send_match_to_pending(interaction.guild, self.match_id)

    async def cancel_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if row["status"] != "awaiting_confirmation":
            await interaction.response.send_message("Essa partida não pode mais ser cancelada.", ephemeral=True)
            return

        uid = str(interaction.user.id)
        details = get_match_players_details(row)
        player_ids = [p["user_id"] for p in details]

        if uid not in player_ids:
            await interaction.response.send_message("Você não faz parte dessa partida.", ephemeral=True)
            return

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'cancelled' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        channel = interaction.guild.get_channel(row["private_channel_id"])
        if isinstance(channel, discord.TextChannel):
            await channel.send("❌ A partida foi cancelada por um dos líderes.")
            await channel.edit(name=f"cancelada-{fmt_match_id(self.match_id)}")

        if row["control_channel_id"]:
            control_channel = interaction.guild.get_channel(row["control_channel_id"])
            if isinstance(control_channel, discord.TextChannel):
                await control_channel.delete(reason="Partida cancelada")

        await send_staff_log(interaction.guild, f"❌ Partida #{fmt_match_id(self.match_id)} cancelada por <@{uid}>.")
        await interaction.response.defer()


class ClaimMatchView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

        claim_btn = discord.ui.Button(
            label="Assumir partida",
            style=discord.ButtonStyle.primary,
            custom_id=f"claim_match_{match_id}"
        )
        claim_btn.callback = self.claim_callback
        self.add_item(claim_btn)

    async def claim_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if row["status"] != "pending":
            await interaction.response.send_message("Essa partida já foi assumida.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not is_adm_member(interaction.user):
            await interaction.response.send_message("Apenas ADMs podem assumir partidas.", ephemeral=True)
            return

        await assume_match(interaction.guild, interaction.user, self.match_id)
        await interaction.response.defer()


class MatchFinalizeView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

        finish_btn = discord.ui.Button(
            label="Finalizar",
            style=discord.ButtonStyle.danger,
            custom_id=f"finish_match_channel_{match_id}"
        )
        finish_btn.callback = self.finish_callback
        self.add_item(finish_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Usuário inválido.", ephemeral=True)
            return False

        if not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável ou o dono podem finalizar.", ephemeral=True)
            return False

        return True

    async def finish_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        try:
            await finalize_match(
                interaction.guild,
                self.match_id,
                interaction.user.mention
            )
        except Exception as e:
            await send_staff_log(
                interaction.guild,
                f"❌ Erro ao finalizar a partida #{fmt_match_id(self.match_id)}: {e}"
            )


class ControlMatchView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

        row = match_row(match_id)
        if not row:
            return

        if row["status"] == "claimed":
            start_btn = discord.ui.Button(
                label="Iniciar",
                style=discord.ButtonStyle.success,
                custom_id=f"start_match_{match_id}"
            )
            start_btn.callback = self.start_callback
            self.add_item(start_btn)

        elif row["status"] == "in_progress":
            pix_btn = discord.ui.Button(
                label="Gerar cobrança",
                style=discord.ButtonStyle.secondary,
                custom_id=f"generate_pix_{match_id}"
            )
            pix_btn.callback = self.generate_pix_callback
            self.add_item(pix_btn)

        elif row["status"] == "payment_pending":
            confirm_btn = discord.ui.Button(
                label="Confirmar pagamento",
                style=discord.ButtonStyle.primary,
                custom_id=f"confirm_payment_{match_id}"
            )
            confirm_btn.callback = self.confirm_payment_callback
            self.add_item(confirm_btn)

        elif row["status"] == "payment_confirmed":
            room_btn = discord.ui.Button(
                label="Iniciar partida",
                style=discord.ButtonStyle.success,
                custom_id=f"start_room_{match_id}"
            )
            room_btn.callback = self.start_room_callback
            self.add_item(room_btn)

        elif row["status"] == "room_sent":
            ss_btn = discord.ui.Button(
                label="Chamar SS",
                style=discord.ButtonStyle.primary,
                custom_id=f"call_ss_{match_id}"
            )
            wo_btn = discord.ui.Button(
                label="W.O.",
                style=discord.ButtonStyle.danger,
                custom_id=f"wo_{match_id}"
            )
            ss_btn.callback = self.call_ss_callback
            wo_btn.callback = self.wo_callback
            self.add_item(ss_btn)
            self.add_item(wo_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return False

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Usuário inválido.", ephemeral=True)
            return False

        if not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável ou o dono do servidor podem usar esse painel.", ephemeral=True)
            return False

        return True

    async def start_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        row = match_row(self.match_id)
        private_channel = interaction.guild.get_channel(row["private_channel_id"])
        if not isinstance(private_channel, discord.TextChannel):
            return

        await private_channel.edit(name=f"em-andamento-{fmt_match_id(self.match_id)}")

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'in_progress' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        await update_pending_message(interaction.guild, self.match_id, "🔴 Em andamento", 0xE74C3C)
        await update_confirmation_message_status(interaction.guild, self.match_id, "🔴 Em andamento", 0xE74C3C)
        await update_control_message(interaction.guild, self.match_id)
        await send_staff_log(interaction.guild, f"▶️ Partida #{fmt_match_id(self.match_id)} iniciada por {interaction.user.mention}")

    async def generate_pix_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PixChargeModal(self.match_id))

    async def confirm_payment_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        row = match_row(self.match_id)
        private_channel = interaction.guild.get_channel(row["private_channel_id"])
        if not isinstance(private_channel, discord.TextChannel):
            return

        panel_value = parse_decimal_brl(str(row["info"]))
        payout_text = format_brl(panel_value)
        payout_channel = format_money_for_channel(panel_value)

        await private_channel.edit(name=f"pagar-{payout_channel}")

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'payment_confirmed' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        await update_pending_message(
            interaction.guild,
            self.match_id,
            f"🟢 Pagamento confirmado • pagar {payout_text}",
            0x2ECC71
        )

        await update_confirmation_message_status(
            interaction.guild,
            self.match_id,
            f"🟢 Pagamento confirmado • pagar {payout_text}",
            0x2ECC71
        )

        await update_control_message(interaction.guild, self.match_id)
        await send_staff_log(
            interaction.guild,
            f"💰 Pagamento confirmado na partida #{fmt_match_id(self.match_id)} por {interaction.user.mention}"
        )

    async def start_room_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RoomInfoModal(self.match_id))

    async def call_ss_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()

        row = match_row(self.match_id)
        if row["analysis_requested"]:
            return

        analises_channel = interaction.guild.get_channel(CANAL_ANALISES_PENDENTES_ID)
        if not isinstance(analises_channel, discord.TextChannel):
            await send_staff_log(interaction.guild, "❌ Canal ANÁLISES PENDENTES não encontrado.")
            return

        msg = await analises_channel.send(
            embed=build_analysis_embed(self.match_id),
            view=AnalysisClaimView(self.match_id)
        )

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE matches
                SET analysis_requested = 1,
                    analysis_message_id = ?
                WHERE match_id = ?
            """, (msg.id, self.match_id))
            conn.commit()

        bot.add_view(AnalysisClaimView(self.match_id), message_id=msg.id)
        await send_staff_log(interaction.guild, f"🧪 Análise solicitada para a partida #{fmt_match_id(self.match_id)} por {interaction.user.mention}.")

    async def wo_callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Escolha o vencedor por W.O.:",
            ephemeral=True,
            view=WOSelectView(self.match_id)
        )


class AnalysisClaimView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

        claim_btn = discord.ui.Button(
            label="Assumir análise",
            style=discord.ButtonStyle.primary,
            custom_id=f"claim_analysis_{match_id}"
        )
        claim_btn.callback = self.claim_callback
        self.add_item(claim_btn)

    async def claim_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not is_ss_member(interaction.user):
            await interaction.response.send_message("Apenas o cargo SS pode assumir análise.", ephemeral=True)
            return

        private_channel = interaction.guild.get_channel(row["private_channel_id"])
        if not isinstance(private_channel, discord.TextChannel):
            await interaction.response.send_message("Canal da partida não encontrado.", ephemeral=True)
            return

        await private_channel.set_permissions(
            interaction.user,
            view_channel=True,
            send_messages=True,
            read_message_history=True
        )

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE matches
                SET analysis_claimed_by = ?
                WHERE match_id = ?
            """, (interaction.user.id, self.match_id))
            conn.commit()

        await private_channel.send(f"🧪 {interaction.user.mention} assumiu a análise da partida.")

        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        await send_staff_log(interaction.guild, f"🧪 {interaction.user.mention} assumiu análise da partida #{fmt_match_id(self.match_id)}.")
        await interaction.response.defer()


class WOSelect(discord.ui.Select):
    def __init__(self, match_id: int):
        self.match_id = match_id
        row = match_row(match_id)
        details = get_match_players_details(row)

        options = []
        for item in details:
            uid = item["user_id"]
            label = f"Vencedor: {uid}"
            description = item.get("selected_option", "")
            options.append(discord.SelectOption(
                label=label[:100],
                value=uid,
                description=description[:100] if description else None
            ))

        super().__init__(
            placeholder="Escolha o vencedor por W.O.",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        winner_id = self.values[0]
        winner_mention = f"<@{winner_id}>"

        await interaction.response.defer()

        await send_staff_log(
            interaction.guild,
            f"🏆 {winner_mention} venceu por W.O. na partida #{fmt_match_id(self.match_id)}."
        )

        private_channel = interaction.guild.get_channel(match_row(self.match_id)["private_channel_id"])
        if isinstance(private_channel, discord.TextChannel):
            await private_channel.send(f"🏆 {winner_mention} venceu por W.O.")

        await finalize_match(
            interaction.guild,
            self.match_id,
            interaction.user.mention,
            reason_text=f"{winner_mention} venceu por W.O."
        )


class WOSelectView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=300)
        self.add_item(WOSelect(match_id))


class PixChargeModal(discord.ui.Modal, title="Gerar cobrança Pix"):
    def __init__(self, match_id: int):
        super().__init__()
        self.match_id = match_id

        self.receiver_name = discord.ui.TextInput(
            label="Nome do recebedor",
            placeholder="Ex.: João Silva",
            max_length=25,
            required=True
        )

        self.pix_key = discord.ui.TextInput(
            label="Chave Pix",
            placeholder="CPF, e-mail, telefone ou chave aleatória",
            max_length=77,
            required=True
        )

        self.amount = discord.ui.TextInput(
            label="Valor que cada líder vai pagar",
            placeholder="Ex.: 0,50",
            max_length=20,
            required=True
        )

        self.add_item(self.receiver_name)
        self.add_item(self.pix_key)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável ou o dono podem gerar cobrança.", ephemeral=True)
            return

        try:
            amount_value = parse_decimal_brl(str(self.amount))
        except (InvalidOperation, ValueError):
            await interaction.response.send_message("Valor inválido. Use por exemplo: 0,50", ephemeral=True)
            return

        pix_key = str(self.pix_key).strip()
        receiver_name = str(self.receiver_name).strip()

        payload = build_pix_payload(
            pix_key=pix_key,
            receiver_name=receiver_name,
            amount=amount_value,
            city=PIX_CIDADE_PADRAO
        )

        qr_file = generate_qr_file(payload)

        private_channel = interaction.guild.get_channel(row["private_channel_id"])
        if not isinstance(private_channel, discord.TextChannel):
            await interaction.response.send_message("Canal da partida não encontrado.", ephemeral=True)
            return

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE matches
                SET status = 'payment_pending',
                    charge_amount = ?,
                    pix_receiver_name = ?,
                    pix_key = ?
                WHERE match_id = ?
            """, (str(amount_value), receiver_name, pix_key, self.match_id))
            conn.commit()

        embed = discord.Embed(
            title=f"💳 Cobrança Pix - Partida #{fmt_match_id(self.match_id)}",
            color=0x2ECC71,
            description=(
                f"👤 **Recebedor:** {receiver_name}\n"
                f"🔑 **Chave Pix:** `{pix_key}`\n"
                f"💸 **Valor por líder:** R$ {format_brl(amount_value)}\n\n"
                f"**Pix copia e cola:**\n```{payload}```"
            )
        )
        embed.set_image(url="attachment://pix_qrcode.png")
        embed.set_footer(text="Envie o comprovante após realizar o pagamento.")

        await private_channel.send(embed=embed, file=qr_file)

        await update_pending_message(interaction.guild, self.match_id, "🟠 Cobrança enviada", 0xF39C12)
        await update_confirmation_message_status(interaction.guild, self.match_id, "🟠 Cobrança enviada", 0xF39C12)
        await update_control_message(interaction.guild, self.match_id)
        await send_staff_log(
            interaction.guild,
            f"💳 Cobrança Pix gerada na partida #{fmt_match_id(self.match_id)} por {interaction.user.mention} no valor individual de R$ {format_brl(amount_value)}."
        )

        await interaction.response.defer()


class RoomInfoModal(discord.ui.Modal, title="Iniciar partida"):
    def __init__(self, match_id: int):
        super().__init__()
        self.match_id = match_id

        self.room_id = discord.ui.TextInput(
            label="ID da sala",
            placeholder="Digite o ID da sala",
            max_length=50,
            required=True
        )

        self.room_password = discord.ui.TextInput(
            label="Senha da sala",
            placeholder="Digite a senha da sala",
            max_length=50,
            required=True
        )

        self.add_item(self.room_id)
        self.add_item(self.room_password)

    async def on_submit(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável ou o dono podem iniciar a partida.", ephemeral=True)
            return

        private_channel = interaction.guild.get_channel(row["private_channel_id"])
        if not isinstance(private_channel, discord.TextChannel):
            await interaction.response.send_message("Canal da partida não encontrado.", ephemeral=True)
            return

        room_id = str(self.room_id).strip()
        room_password = str(self.room_password).strip()

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE matches
                SET status = 'room_sent',
                    room_id = ?,
                    room_password = ?
                WHERE match_id = ?
            """, (room_id, room_password, self.match_id))
            conn.commit()

        details = get_match_players_details(match_row(self.match_id))
        selected_option = details[0].get("selected_option", "") if details else ""

        embed = discord.Embed(
            title=f"🎮 Sala criada - Partida #{fmt_match_id(self.match_id)}",
            color=0x3498DB,
            description=(
                f"📌 **Configuração:** {selected_option}\n"
                f"🆔 **ID da sala:** `{room_id}`\n"
                f"🔐 **Senha:** `{room_password}`\n\n"
                f"Boa partida!"
            )
        )

        await private_channel.send(embed=embed)

        finish_msg = await private_channel.send(
            "⚠️ **Finalize somente após a partida terminar e o pagamento do vencedor ser realizado.**",
            view=MatchFinalizeView(self.match_id)
        )

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE matches SET finish_message_id = ? WHERE match_id = ?",
                (finish_msg.id, self.match_id)
            )
            conn.commit()

        bot.add_view(MatchFinalizeView(self.match_id), message_id=finish_msg.id)

        await update_control_message(interaction.guild, self.match_id)
        await send_staff_log(interaction.guild, f"🎮 Sala enviada na partida #{fmt_match_id(self.match_id)} por {interaction.user.mention}.")
        await interaction.response.defer()


# =========================
# MATCH FLOW
# =========================
async def try_create_match_from_queue(guild: discord.Guild, panel_id: str):
    players = panel_players(panel_id)
    if len(players) < 2:
        return

    groups = {}
    for item in players:
        key = item["selected_option"]
        groups.setdefault(key, []).append(item)

    matched_players = None
    for _, group in groups.items():
        if len(group) >= 2:
            matched_players = group[:2]
            break

    if not matched_players:
        return

    await create_match_confirmation_room(guild, panel_id, matched_players)
    await refresh_panel_message(panel_id)
    await try_create_match_from_queue(guild, panel_id)


async def create_match_confirmation_room(guild: discord.Guild, panel_id: str, matched_players: list[dict]):
    panel = panel_row(panel_id)
    if not panel:
        return

    em_andamento_cat = find_category(guild, NOME_CATEGORIA_EM_ANDAMENTO)
    controle_cat = find_category(guild, NOME_CATEGORIA_CONTROLE)
    owner = guild.owner

    match_id = next_match_id()

    overwrites_partida = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_permissions=True
        )
    }

    overwrites_controle = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_permissions=True
        )
    }

    if owner:
        owner_perm = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        )
        overwrites_partida[owner] = owner_perm
        overwrites_controle[owner] = owner_perm

    for item in matched_players:
        uid = item["user_id"]
        member = guild.get_member(int(uid))
        if member is None:
            try:
                member = await guild.fetch_member(int(uid))
            except discord.NotFound:
                member = None

        if member:
            overwrites_partida[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

    private_channel = await guild.create_text_channel(
        name=f"fila-{fmt_match_id(match_id)}",
        category=em_andamento_cat,
        overwrites=overwrites_partida,
        reason="Canal de partida criado automaticamente"
    )

    control_channel = await guild.create_text_channel(
        name=f"controle-{fmt_match_id(match_id)}",
        category=controle_cat,
        overwrites=overwrites_controle,
        reason="Canal de controle criado automaticamente"
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO matches (
                match_id, panel_id, guild_id, title, mode, info, status,
                players_details_json, confirmed_players_json,
                private_channel_id, control_channel_id,
                pending_message_id, confirmation_message_id, control_message_id,
                finish_message_id, claimed_by, charge_amount, pix_receiver_name, pix_key,
                room_id, room_password, analysis_requested, analysis_message_id, analysis_claimed_by
            ) VALUES (?, ?, ?, ?, ?, ?, 'awaiting_confirmation', ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL)
        """, (
            match_id,
            panel_id,
            guild.id,
            panel["title"],
            panel["mode"],
            panel["info"],
            json.dumps(matched_players),
            json.dumps([]),
            private_channel.id,
            control_channel.id
        ))

        for item in matched_players:
            cur.execute(
                "DELETE FROM panel_players WHERE panel_id = ? AND user_id = ?",
                (panel_id, item["user_id"])
            )

        conn.commit()

    confirmation_msg = await private_channel.send(
        embed=build_confirmation_embed(match_id),
        view=PlayerConfirmationView(match_id)
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE matches SET confirmation_message_id = ? WHERE match_id = ?",
            (confirmation_msg.id, match_id)
        )
        conn.commit()

    bot.add_view(PlayerConfirmationView(match_id), message_id=confirmation_msg.id)
    await send_staff_log(guild, f"🆕 Partida #{fmt_match_id(match_id)} criada aguardando confirmação dos líderes.")


async def send_match_to_pending(guild: discord.Guild, match_id: int):
    row = match_row(match_id)
    if not row or row["status"] != "awaiting_confirmation":
        return

    pending_channel = guild.get_channel(CANAL_PARTIDAS_PENDENTES_ID)
    if not isinstance(pending_channel, discord.TextChannel):
        return

    msg = await pending_channel.send(
        embed=build_pending_match_embed(match_id),
        view=ClaimMatchView(match_id)
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE matches SET status = 'pending', pending_message_id = ? WHERE match_id = ?",
            (msg.id, match_id)
        )
        conn.commit()

    private_channel = guild.get_channel(row["private_channel_id"])
    if isinstance(private_channel, discord.TextChannel):
        await private_channel.send("✅ Os dois líderes confirmaram. Aguardando ADM assumir a partida.")

    await update_confirmation_message_status(guild, match_id, "🟡 Aguardando ADM", 0x3498DB)
    bot.add_view(ClaimMatchView(match_id), message_id=msg.id)
    await send_staff_log(guild, f"📥 Partida #{fmt_match_id(match_id)} enviada para pendentes.")


async def assume_match(guild: discord.Guild, adm: discord.Member, match_id: int):
    row = match_row(match_id)
    if not row or row["status"] != "pending":
        return

    control_channel = guild.get_channel(row["control_channel_id"])
    private_channel = guild.get_channel(row["private_channel_id"])

    if not isinstance(control_channel, discord.TextChannel) or not isinstance(private_channel, discord.TextChannel):
        return

    await control_channel.set_permissions(
        adm,
        view_channel=True,
        send_messages=True,
        read_message_history=True
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE matches SET status = 'claimed', claimed_by = ? WHERE match_id = ?",
            (adm.id, match_id)
        )
        conn.commit()

    details = get_match_players_details(match_row(match_id))

    control_msg = await control_channel.send(
        content=(
            f"🎮 **Partida #{fmt_match_id(match_id)}**\n"
            f"👤 **Líderes:** {build_players_vs(details)}\n"
            f"📌 **Status:** Em preparação"
        ),
        view=ControlMatchView(match_id)
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE matches SET control_message_id = ? WHERE match_id = ?",
            (control_msg.id, match_id)
        )
        conn.commit()

    bot.add_view(ControlMatchView(match_id), message_id=control_msg.id)

    await private_channel.send(f"✅ {adm.mention} assumiu a partida.")
    await update_pending_message(guild, match_id, "🟡 Em preparação", 0xF1C40F)
    await update_confirmation_message_status(guild, match_id, "🟡 Em preparação", 0xF1C40F)
    await send_staff_log(guild, f"🙋 Partida #{fmt_match_id(match_id)} assumida por {adm.mention}.")


# =========================
# PANEL CREATION
# =========================
async def criar_painel_individual(message: discord.Message, titulo: str, modo: str, info: str):
    embed = discord.Embed(title=titulo, color=0x2ECC71)
    embed.description = (
        f"🎮 **Modo:**\n{modo}\n\n"
        f"💸 **Valor:**\n{info}\n\n"
        f"👤 **Líderes na fila:**\nNenhum jogador na fila"
    )
    embed.set_thumbnail(url=IMAGEM_PADRAO)

    msg = await message.channel.send(embed=embed)
    panel_id = str(msg.id)

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO panels (
                panel_id, guild_id, channel_id, title, mode, info, image_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            panel_id,
            message.guild.id,
            message.channel.id,
            titulo,
            modo,
            info,
            IMAGEM_PADRAO
        ))
        conn.commit()

    view = PanelQueueView(panel_id)
    await msg.edit(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)


async def processar_multiplos_paineis(message: discord.Message) -> bool:
    linhas = [linha.strip() for linha in message.content.splitlines() if linha.strip()]

    if not linhas:
        return False

    if not all(linha.startswith("!painelz") for linha in linhas):
        return False

    criados = 0
    erros = []

    for i, linha in enumerate(linhas, start=1):
        try:
            partes = shlex.split(linha)
        except ValueError as e:
            erros.append(f"Linha {i}: erro nas aspas -> {e}")
            continue

        if len(partes) != 4 or partes[0] != "!painelz":
            erros.append(f"Linha {i}: use exatamente !painelz \"titulo\" \"modo\" \"valor\"")
            continue

        _, titulo, modo, info = partes

        try:
            await criar_painel_individual(message, titulo, modo, info)
            criados += 1
        except Exception as e:
            erros.append(f"Linha {i}: {e}")

    if criados > 0:
        texto = f"✅ {criados} painel(is) criado(s) com sucesso."
        if erros:
            texto += "\n⚠️ Erros:\n- " + "\n- ".join(erros)
        await message.channel.send(texto)
    else:
        await message.channel.send(
            "❌ Nenhum painel foi criado.\n"
            "Use assim, uma linha por painel:\n"
            "`!painelz \"1x1 LAGARTISSE ELITE\" \"1x1 Mobile\" \"R$100,00\"`"
        )

    return True


# =========================
# COMMANDS
# =========================
@bot.command()
@commands.has_permissions(administrator=True)
async def limparcanal(ctx):
    await ctx.channel.purge()


# =========================
# EVENTS
# =========================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is None:
        return

    processado = await processar_multiplos_paineis(message)
    if processado:
        return

    await bot.process_commands(message)


@bot.event
async def on_ready():
    db_init()

    with closing(db_connect()) as conn:
        cur = conn.cursor()

        cur.execute("SELECT panel_id FROM panels")
        for row in cur.fetchall():
            try:
                bot.add_view(PanelQueueView(row["panel_id"]), message_id=int(row["panel_id"]))
            except Exception:
                pass

        cur.execute("""
            SELECT match_id, confirmation_message_id
            FROM matches
            WHERE status = 'awaiting_confirmation' AND confirmation_message_id IS NOT NULL
        """)
        for row in cur.fetchall():
            try:
                bot.add_view(PlayerConfirmationView(row["match_id"]), message_id=row["confirmation_message_id"])
            except Exception:
                pass

        cur.execute("""
            SELECT match_id, pending_message_id
            FROM matches
            WHERE status = 'pending' AND pending_message_id IS NOT NULL
        """)
        for row in cur.fetchall():
            try:
                bot.add_view(ClaimMatchView(row["match_id"]), message_id=row["pending_message_id"])
            except Exception:
                pass

        cur.execute("""
            SELECT match_id, control_message_id
            FROM matches
            WHERE status IN ('claimed', 'in_progress', 'payment_pending', 'payment_confirmed', 'room_sent')
              AND control_message_id IS NOT NULL
        """)
        for row in cur.fetchall():
            try:
                bot.add_view(ControlMatchView(row["match_id"]), message_id=row["control_message_id"])
            except Exception:
                pass

        cur.execute("""
            SELECT match_id, finish_message_id
            FROM matches
            WHERE status = 'room_sent' AND finish_message_id IS NOT NULL
        """)
        for row in cur.fetchall():
            try:
                bot.add_view(MatchFinalizeView(row["match_id"]), message_id=row["finish_message_id"])
            except Exception:
                pass

        cur.execute("""
            SELECT match_id, analysis_message_id
            FROM matches
            WHERE analysis_requested = 1
              AND analysis_message_id IS NOT NULL
              AND analysis_claimed_by IS NULL
        """)
        for row in cur.fetchall():
            try:
                bot.add_view(AnalysisClaimView(row["match_id"]), message_id=row["analysis_message_id"])
            except Exception:
                pass

    print(f"Bot ligado como {bot.user}")


db_init()
bot.run(os.getenv("DISCORD_TOKEN"))
