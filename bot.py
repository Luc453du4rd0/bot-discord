import os
import io
import re
import json
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

NOME_CARGO_ADM = "ADM"
NOME_CATEGORIA_EM_ANDAMENTO = "EM ANDAMENTO"
NOME_CATEGORIA_FINALIZADAS = "FINALIZADAS"

# Cidade fixa do BR Code Pix (não vai no formulário)
PIX_CIDADE_PADRAO = "TAVARES"

DB_FILE = "bot_data.sqlite3"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


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
            image_url TEXT NOT NULL,
            max_players INTEGER NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS panel_players (
            panel_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
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
            players_json TEXT NOT NULL,
            confirmed_players_json TEXT NOT NULL,
            status TEXT NOT NULL,
            private_channel_id INTEGER,
            pending_message_id INTEGER,
            control_message_id INTEGER,
            confirmation_message_id INTEGER,
            claimed_by INTEGER
        )
        """)

        cur.execute("SELECT value FROM meta WHERE key = 'counter'")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO meta (key, value) VALUES ('counter', '0')")

        conn.commit()

    # Migrações simples para compatibilidade
    ensure_column_exists("matches", "claimed_by", "INTEGER")
    ensure_column_exists("matches", "control_message_id", "INTEGER")
    ensure_column_exists("matches", "pending_message_id", "INTEGER")
    ensure_column_exists("matches", "confirmation_message_id", "INTEGER")
    ensure_column_exists("matches", "private_channel_id", "INTEGER")


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


def find_role(guild: discord.Guild, role_name: str):
    return discord.utils.get(guild.roles, name=role_name)


def find_category(guild: discord.Guild, category_name: str):
    return discord.utils.get(guild.categories, name=category_name)


def define_max_players(mode: str) -> int:
    mode_clean = mode.lower().strip()
    if mode_clean in ["1x1", "1v1"]:
        return 2
    if mode_clean in ["2x2", "2v2"]:
        return 4
    if mode_clean in ["3x3", "3v3"]:
        return 6
    if mode_clean in ["4x4", "4v4"]:
        return 8
    return 2


def mention_list_from_ids(user_ids: list[str]) -> str:
    return " vs ".join([f"<@{uid}>" for uid in user_ids])


def is_adm_member(member: discord.Member) -> bool:
    return any(role.name.lower() == NOME_CARGO_ADM.lower() for role in member.roles)


def panel_row(panel_id: str):
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM panels WHERE panel_id = ?", (panel_id,))
        return cur.fetchone()


def panel_players(panel_id: str) -> list[str]:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM panel_players WHERE panel_id = ? ORDER BY rowid ASC", (panel_id,))
        return [r["user_id"] for r in cur.fetchall()]


def match_row(match_id: int):
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM matches WHERE match_id = ?", (match_id,))
        return cur.fetchone()


def active_match_for_panel(panel_id: str) -> bool:
    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM matches WHERE panel_id = ? AND status IN ('awaiting_confirmation', 'pending', 'claimed', 'in_progress') LIMIT 1",
            (panel_id,)
        )
        return cur.fetchone() is not None


def parse_decimal_brl(value_str: str) -> Decimal:
    value_str = value_str.strip().replace("R$", "").replace(" ", "")
    value_str = value_str.replace(".", "").replace(",", ".")
    value = Decimal(value_str)
    if value <= 0:
        raise InvalidOperation
    return value.quantize(Decimal("0.01"))


def format_brl(value: Decimal) -> str:
    return f"R$ {str(value.quantize(Decimal('0.01'))).replace('.', ',')}"


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
        emv("00", "01") +                     # Payload Format Indicator
        emv("26", merchant_account_info) +   # Merchant Account Information
        emv("52", "0000") +                  # Merchant Category Code
        emv("53", "986") +                   # Currency BRL
        emv("54", amount_str) +              # Amount
        emv("58", "BR") +                    # Country Code
        emv("59", receiver_name) +           # Merchant Name
        emv("60", city) +                    # Merchant City
        emv("62", additional_data) +         # Additional Data Field
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


def can_manage_match(member: discord.Member, row) -> bool:
    if not is_adm_member(member):
        return False

    claimed_by = row["claimed_by"]
    if claimed_by is None:
        return True

    return int(claimed_by) == member.id


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
    players_text = "\n".join([f"<@{uid}>" for uid in players]) if players else "Nenhum jogador na fila"

    embed = discord.Embed(title=panel["title"], color=0x2ECC71)
    embed.description = (
        f"🎮 **Modo:**\n{panel['mode']}\n\n"
        f"💸 **Valor:**\n{panel['info']}\n\n"
        f"👤 **Jogadores:**\n{players_text}"
    )
    embed.set_thumbnail(url=panel["image_url"])
    return embed


def build_confirmation_embed(match_id: int) -> discord.Embed:
    row = match_row(match_id)
    players = json.loads(row["players_json"])
    confirmed = json.loads(row["confirmed_players_json"])

    status_lines = []
    for uid in players:
        if uid in confirmed:
            status_lines.append(f"✅ <@{uid}> confirmou")
        else:
            status_lines.append(f"⏳ <@{uid}> aguardando")

    embed = discord.Embed(
        title=f"Partida #{fmt_match_id(match_id)}",
        color=0xF1C40F,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"💸 **Valor:** {row['info']}\n"
            f"👤 **Jogadores:** {mention_list_from_ids(players)}\n"
            f"📌 **Status:** Aguardando confirmação\n\n"
            + "\n".join(status_lines)
        )
    )
    return embed


def build_match_status_embed(match_id: int, status_text: str, color: int, adm_mention: str | None = None) -> discord.Embed:
    row = match_row(match_id)
    players = json.loads(row["players_json"])

    extra = f"\n✅ **Assumida por:** {adm_mention}" if adm_mention else ""

    embed = discord.Embed(
        title=f"Partida #{fmt_match_id(match_id)}",
        color=color,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"💸 **Valor:** {row['info']}\n"
            f"👤 **Jogadores:** {mention_list_from_ids(players)}"
            f"{extra}\n"
            f"📌 **Status:** {status_text}"
        )
    )
    return embed


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
        await msg.edit(embed=build_panel_embed(panel_id), view=PanelQueueView(panel_id))
    except discord.NotFound:
        pass


async def refresh_confirmation_message(guild: discord.Guild, match_id: int):
    row = match_row(match_id)
    if not row or not row["confirmation_message_id"] or not row["private_channel_id"]:
        return

    channel = guild.get_channel(row["private_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["confirmation_message_id"])
        await msg.edit(embed=build_confirmation_embed(match_id), view=PlayerConfirmationView(match_id))
    except discord.NotFound:
        pass


async def update_confirmation_message_status(guild: discord.Guild, match_id: int, status: str, color: int = 0x2ECC71):
    row = match_row(match_id)
    if not row or not row["confirmation_message_id"] or not row["private_channel_id"]:
        return

    channel = guild.get_channel(row["private_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["confirmation_message_id"])
        players = json.loads(row["players_json"])

        adm_mention = f"<@{row['claimed_by']}>" if row["claimed_by"] else None
        embed = discord.Embed(
            title=f"Partida #{fmt_match_id(match_id)}",
            color=color,
            description=(
                f"🎮 **Modo:** {row['mode']}\n"
                f"💸 **Valor:** {row['info']}\n"
                f"👤 **Jogadores:** {mention_list_from_ids(players)}\n"
                + (f"✅ **Assumida por:** {adm_mention}\n" if adm_mention else "")
                + f"📌 **Status:** {status}"
            )
        )

        await msg.edit(embed=embed, view=None)
    except discord.NotFound:
        pass


async def update_pending_message_embed(guild: discord.Guild, match_id: int, status: str, color: int, remove_view: bool = True):
    row = match_row(match_id)
    if not row or not row["pending_message_id"]:
        return

    channel = guild.get_channel(CANAL_PARTIDAS_PENDENTES_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["pending_message_id"])
        adm_mention = f"<@{row['claimed_by']}>" if row["claimed_by"] else None
        embed = build_match_status_embed(match_id, status, color, adm_mention=adm_mention)
        await msg.edit(embed=embed, view=None if remove_view else ClaimMatchView(match_id))
    except discord.NotFound:
        pass


async def update_control_message(guild: discord.Guild, match_id: int):
    row = match_row(match_id)
    if not row or not row["control_message_id"] or not row["private_channel_id"]:
        return

    channel = guild.get_channel(row["private_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["control_message_id"])
    except discord.NotFound:
        return

    status_map = {
        "claimed": "🟡 Em preparação",
        "in_progress": "🔴 Em andamento",
        "finished": "✅ Finalizada"
    }
    status_text = status_map.get(row["status"], row["status"])
    players = json.loads(row["players_json"])
    adm_mention = f"<@{row['claimed_by']}>" if row["claimed_by"] else "Não definido"

    content = (
        f"🎮 **Painel da Partida #{fmt_match_id(match_id)}**\n"
        f"ADM responsável: {adm_mention}\n"
        f"Jogadores: {mention_list_from_ids(players)}\n"
        f"Status atual: {status_text}\n\n"
        f"O ADM responsável pode abrir o painel privado no botão abaixo."
    )

    if row["status"] == "finished":
        await msg.edit(content=content, view=None)
    else:
        await msg.edit(content=content, view=AdminAccessView(match_id))


# =========================
# VIEWS
# =========================
class PanelQueueView(discord.ui.View):
    def __init__(self, panel_id: str):
        super().__init__(timeout=None)
        self.panel_id = panel_id

        join_btn = discord.ui.Button(
            label="Entrar na fila",
            style=discord.ButtonStyle.success,
            custom_id=f"panel_join_{panel_id}"
        )
        leave_btn = discord.ui.Button(
            label="Sair da fila",
            style=discord.ButtonStyle.danger,
            emoji="🔻",
            custom_id=f"panel_leave_{panel_id}"
        )

        join_btn.callback = self.join_callback
        leave_btn.callback = self.leave_callback

        self.add_item(join_btn)
        self.add_item(leave_btn)

    async def join_callback(self, interaction: discord.Interaction):
        panel = panel_row(self.panel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return

        if active_match_for_panel(self.panel_id):
            await interaction.response.send_message("Já existe uma partida desse painel em andamento ou pendente.", ephemeral=True)
            return

        uid = str(interaction.user.id)
        players = panel_players(self.panel_id)

        if uid in players:
            await interaction.response.send_message("Você já está nessa fila.", ephemeral=True)
            return

        if len(players) >= panel["max_players"]:
            await interaction.response.send_message("Fila cheia.", ephemeral=True)
            return

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO panel_players (panel_id, user_id) VALUES (?, ?)", (self.panel_id, uid))
            conn.commit()

        await interaction.response.defer()
        await refresh_panel_message(self.panel_id)

        players = panel_players(self.panel_id)
        if len(players) == panel["max_players"]:
            await create_match_confirmation_room(interaction.guild, self.panel_id)

    async def leave_callback(self, interaction: discord.Interaction):
        panel = panel_row(self.panel_id)
        if not panel:
            await interaction.response.send_message("Painel não encontrado.", ephemeral=True)
            return

        uid = str(interaction.user.id)
        players = panel_players(self.panel_id)

        if uid not in players:
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
        players = json.loads(row["players_json"])

        if uid not in players:
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

        if len(confirmed) == len(players):
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
        players = json.loads(row["players_json"])

        if uid not in players:
            await interaction.response.send_message("Você não faz parte dessa partida.", ephemeral=True)
            return

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'cancelled' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        channel = interaction.guild.get_channel(row["private_channel_id"])
        if isinstance(channel, discord.TextChannel):
            await channel.send("❌ A partida foi cancelada por um dos jogadores.")
            await channel.edit(name=f"cancelada-{fmt_match_id(self.match_id)}")

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

        await interaction.response.send_message(
            f"Você assumiu a partida #{fmt_match_id(self.match_id)}. O painel privado foi liberado para você no canal da partida.",
            ephemeral=True
        )


class AdminAccessView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

        open_btn = discord.ui.Button(
            label="Abrir painel ADM",
            style=discord.ButtonStyle.primary,
            custom_id=f"open_admin_panel_{match_id}"
        )
        open_btn.callback = self.open_callback
        self.add_item(open_btn)

    async def open_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Usuário inválido.", ephemeral=True)
            return

        if not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável pode usar esse painel.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Painel privado da partida #{fmt_match_id(self.match_id)}:",
            ephemeral=True,
            view=AdminEphemeralControlView(self.match_id)
        )


class AdminEphemeralControlView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=300)
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

        if row["status"] in ("claimed", "in_progress"):
            pix_btn = discord.ui.Button(
                label="Gerar cobrança",
                style=discord.ButtonStyle.secondary,
                custom_id=f"generate_pix_{match_id}"
            )
            pix_btn.callback = self.generate_pix_callback
            self.add_item(pix_btn)

        if row["status"] == "in_progress":
            finish_btn = discord.ui.Button(
                label="Finalizar",
                style=discord.ButtonStyle.danger,
                custom_id=f"finish_match_{match_id}"
            )
            finish_btn.callback = self.finish_callback
            self.add_item(finish_btn)

    async def start_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if row["status"] != "claimed":
            await interaction.response.send_message("Essa partida não está em preparação.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável pode iniciar.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(row["private_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal da partida não encontrado.", ephemeral=True)
            return

        await channel.edit(name=f"em-andamento-{fmt_match_id(self.match_id)}")

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'in_progress' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        await update_pending_message_embed(interaction.guild, self.match_id, "🔴 Em andamento", 0xE74C3C)
        await update_confirmation_message_status(interaction.guild, self.match_id, "🔴 Em andamento", 0xE74C3C)
        await update_control_message(interaction.guild, self.match_id)

        await send_staff_log(interaction.guild, f"▶️ Partida #{fmt_match_id(self.match_id)} iniciada por {interaction.user.mention}")
        await interaction.response.send_message("Partida iniciada com sucesso.", ephemeral=True)

    async def finish_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if row["status"] != "in_progress":
            await interaction.response.send_message("Essa partida não está em andamento.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável pode finalizar.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(row["private_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal inválido.", ephemeral=True)
            return

        players = json.loads(row["players_json"])
        final_cat = find_category(interaction.guild, NOME_CATEGORIA_FINALIZADAS)

        if final_cat:
            await channel.edit(name=f"finalizada-{fmt_match_id(self.match_id)}", category=final_cat)
        else:
            await channel.edit(name=f"finalizada-{fmt_match_id(self.match_id)}")

        for uid in players:
            member = interaction.guild.get_member(int(uid))
            if member:
                await channel.set_permissions(member, overwrite=None)

        await channel.set_permissions(interaction.guild.default_role, view_channel=False)

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'finished' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        await update_pending_message_embed(interaction.guild, self.match_id, "✅ Finalizada", 0x95A5A6)
        await update_confirmation_message_status(interaction.guild, self.match_id, "✅ Finalizada", 0x95A5A6)
        await update_control_message(interaction.guild, self.match_id)

        await send_staff_log(interaction.guild, f"✅ Partida #{fmt_match_id(self.match_id)} finalizada por {interaction.user.mention}")
        await interaction.response.send_message("Partida finalizada com sucesso.", ephemeral=True)

    async def generate_pix_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not can_manage_match(interaction.user, row):
            await interaction.response.send_message("Apenas o ADM responsável pode gerar cobrança.", ephemeral=True)
            return

        await interaction.response.send_modal(PixChargeModal(self.match_id))


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
            label="Valor a receber",
            placeholder="Ex.: 5,00",
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
            await interaction.response.send_message("Apenas o ADM responsável pode gerar cobrança.", ephemeral=True)
            return

        try:
            amount_value = parse_decimal_brl(str(self.amount))
        except (InvalidOperation, ValueError):
            await interaction.response.send_message("Valor inválido. Use por exemplo: 5,00", ephemeral=True)
            return

        pix_key = str(self.pix_key).strip()
        receiver_name = str(self.receiver_name).strip()

        if not pix_key:
            await interaction.response.send_message("A chave Pix é obrigatória.", ephemeral=True)
            return

        payload = build_pix_payload(
            pix_key=pix_key,
            receiver_name=receiver_name,
            amount=amount_value,
            city=PIX_CIDADE_PADRAO
        )

        qr_file = generate_qr_file(payload)
        channel = interaction.guild.get_channel(row["private_channel_id"])

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal da partida não encontrado.", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"💳 Cobrança Pix - Partida #{fmt_match_id(self.match_id)}",
            color=0x2ECC71,
            description=(
                f"👤 **Recebedor:** {receiver_name}\n"
                f"🔑 **Chave Pix:** `{pix_key}`\n"
                f"💸 **Valor:** {format_brl(amount_value)}\n\n"
                f"**Pix copia e cola:**\n```{payload}```"
            )
        )
        embed.set_image(url="attachment://pix_qrcode.png")
        embed.set_footer(text="Envie o comprovante após realizar o pagamento.")

        await channel.send(embed=embed, file=qr_file)
        await send_staff_log(
            interaction.guild,
            f"💳 Cobrança Pix gerada na partida #{fmt_match_id(self.match_id)} por {interaction.user.mention} no valor de {format_brl(amount_value)}."
        )

        await interaction.response.send_message("Cobrança Pix enviada no canal da partida.", ephemeral=True)


# =========================
# MATCH FLOW
# =========================
async def create_match_confirmation_room(guild: discord.Guild, panel_id: str):
    panel = panel_row(panel_id)
    if not panel:
        return

    players = panel_players(panel_id)
    if len(players) < panel["max_players"]:
        return

    em_andamento_cat = find_category(guild, NOME_CATEGORIA_EM_ANDAMENTO)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True
        )
    }

    for uid in players:
        member = guild.get_member(int(uid))
        if member is None:
            try:
                member = await guild.fetch_member(int(uid))
            except discord.NotFound:
                member = None

        if member:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

    match_id = next_match_id()

    private_channel = await guild.create_text_channel(
        name=f"fila-{fmt_match_id(match_id)}",
        category=em_andamento_cat,
        overwrites=overwrites,
        reason="Canal de confirmação criado automaticamente"
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO matches (
                match_id, panel_id, guild_id, title, mode, info,
                players_json, confirmed_players_json, status,
                private_channel_id, pending_message_id, control_message_id,
                confirmation_message_id, claimed_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_confirmation', ?, NULL, NULL, NULL, NULL)
        """, (
            match_id,
            panel_id,
            guild.id,
            panel["title"],
            panel["mode"],
            panel["info"],
            json.dumps(players),
            json.dumps([]),
            private_channel.id
        ))

        cur.execute("DELETE FROM panel_players WHERE panel_id = ?", (panel_id,))
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
    await refresh_panel_message(panel_id)
    await send_staff_log(guild, f"🆕 Partida #{fmt_match_id(match_id)} criada aguardando confirmação dos jogadores.")


async def send_match_to_pending(guild: discord.Guild, match_id: int):
    row = match_row(match_id)
    if not row or row["status"] != "awaiting_confirmation":
        return

    pending_channel = guild.get_channel(CANAL_PARTIDAS_PENDENTES_ID)
    if not isinstance(pending_channel, discord.TextChannel):
        return

    msg = await pending_channel.send(
        embed=build_match_status_embed(match_id, "🟡 Aguardando ADM", 0x3498DB),
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
        await private_channel.send("✅ Os jogadores confirmaram. Aguardando ADM assumir a partida.")

    await update_confirmation_message_status(guild, match_id, "🟡 Aguardando ADM", 0x3498DB)

    bot.add_view(ClaimMatchView(match_id), message_id=msg.id)
    await send_staff_log(guild, f"📥 Partida #{fmt_match_id(match_id)} enviada para pendentes.")


async def assume_match(guild: discord.Guild, adm: discord.Member, match_id: int):
    row = match_row(match_id)
    if not row or row["status"] != "pending":
        return

    players = json.loads(row["players_json"])
    private_channel = guild.get_channel(row["private_channel_id"])
    adm_role = find_role(guild, NOME_CARGO_ADM)

    if not isinstance(private_channel, discord.TextChannel):
        return

    if adm_role:
        await private_channel.set_permissions(
            adm_role,
            view_channel=True,
            send_messages=True,
            read_message_history=True
        )

    await private_channel.set_permissions(
        adm,
        view_channel=True,
        send_messages=True,
        read_message_history=True
    )

    control_msg = await private_channel.send(
        f"🎮 **Painel da Partida #{fmt_match_id(match_id)}**\n"
        f"ADM responsável: {adm.mention}\n"
        f"Jogadores: {mention_list_from_ids(players)}\n"
        f"Status atual: 🟡 Em preparação\n\n"
        f"O ADM responsável pode abrir o painel privado no botão abaixo.",
        view=AdminAccessView(match_id)
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE matches
            SET status = 'claimed',
                claimed_by = ?,
                control_message_id = ?
            WHERE match_id = ?
        """, (adm.id, control_msg.id, match_id))
        conn.commit()

    bot.add_view(AdminAccessView(match_id), message_id=control_msg.id)

    await update_pending_message_embed(guild, match_id, "🟡 Em preparação", 0xF1C40F)
    await update_confirmation_message_status(guild, match_id, "🟡 Em preparação", 0xF1C40F)

    await private_channel.send(f"✅ {adm.mention} assumiu a partida. O painel de controle foi liberado para o ADM responsável.")
    await send_staff_log(guild, f"🙋 Partida #{fmt_match_id(match_id)} assumida por {adm.mention}.")


# =========================
# COMMANDS
# =========================
@bot.command()
async def painelz(ctx, titulo: str, modo: str, info: str):
    max_players = define_max_players(modo)

    embed = discord.Embed(title=titulo, color=0x2ECC71)
    embed.description = (
        f"🎮 **Modo:**\n{modo}\n\n"
        f"💸 **Valor:**\n{info}\n\n"
        f"👤 **Jogadores:**\nNenhum jogador na fila"
    )
    embed.set_thumbnail(url=IMAGEM_PADRAO)

    msg = await ctx.send(embed=embed)
    panel_id = str(msg.id)

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO panels (
                panel_id, guild_id, channel_id, title, mode, info, image_url, max_players
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            panel_id,
            ctx.guild.id,
            ctx.channel.id,
            titulo,
            modo,
            info,
            IMAGEM_PADRAO,
            max_players
        ))
        conn.commit()

    view = PanelQueueView(panel_id)
    await msg.edit(embed=embed, view=view)
    bot.add_view(view, message_id=msg.id)


# =========================
# READY
# =========================
@bot.event
async def on_ready():
    db_init()

    with closing(db_connect()) as conn:
        cur = conn.cursor()

        # Painéis antigos continuam funcionando
        cur.execute("SELECT panel_id FROM panels")
        for row in cur.fetchall():
            try:
                bot.add_view(PanelQueueView(row["panel_id"]), message_id=int(row["panel_id"]))
            except Exception:
                pass

        # Partidas aguardando confirmação
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

        # Partidas pendentes de ADM
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

        # Partidas assumidas ou em andamento
        cur.execute("""
            SELECT match_id, control_message_id
            FROM matches
            WHERE status IN ('claimed', 'in_progress') AND control_message_id IS NOT NULL
        """)
        for row in cur.fetchall():
            try:
                bot.add_view(AdminAccessView(row["match_id"]), message_id=row["control_message_id"])
            except Exception:
                pass

    print(f"Bot ligado como {bot.user}")


db_init()
bot.run(os.getenv("DISCORD_TOKEN"))
