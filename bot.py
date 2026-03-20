import os
import json
import sqlite3
from contextlib import closing

import discord
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

ROTULO_INFO = "💸 **Valor:"
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
            status TEXT NOT NULL,
            pending_message_id INTEGER,
            claimed_by INTEGER,
            private_channel_id INTEGER,
            control_message_id INTEGER
        )
        """)

        cur.execute("SELECT value FROM meta WHERE key = 'counter'")
        row = cur.fetchone()
        if row is None:
            cur.execute("INSERT INTO meta (key, value) VALUES ('counter', '0')")

        conn.commit()


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
            "SELECT 1 FROM matches WHERE panel_id = ? AND status IN ('pending', 'claimed', 'in_progress') LIMIT 1",
            (panel_id,)
        )
        return cur.fetchone() is not None

async def update_pending_message_status(guild: discord.Guild, match_id: int, status: str):
    row = match_row(match_id)
    if not row or not row["pending_message_id"]:
        return

    channel = guild.get_channel(CANAL_PARTIDAS_PENDENTES_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(row["pending_message_id"])

        players = json.loads(row["players_json"])

        embed = discord.Embed(
            title=f"Partida #{fmt_match_id(match_id)}",
            color=0x95A5A6,
            description=(
                f"🎮 **Modo:** {row['mode']}\n"
                f"👤 **Jogadores:** {mention_list_from_ids(players)}\n"
                f"📌 **Status:** {status}"
            )
        )

        await msg.edit(embed=embed, view=None)

    except discord.NotFound:
        pass

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
         f"💸 **Valor:**\n"
        f"👤 **Jogadores:**\n{players_text}"
    )
    embed.set_thumbnail(url=panel["image_url"])
    return embed


def build_pending_match_embed(match_id: int) -> discord.Embed:
    row = match_row(match_id)
    players = json.loads(row["players_json"])

    embed = discord.Embed(
        title=f"Partida #{fmt_match_id(match_id)}",
        color=0x3498DB,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"💸 **Valor:**\n"
            f"👤 **Jogadores:** {mention_list_from_ids(players)}\n"
            f"📌 **Status:** 🟡 Pendente"
        )
    )
    return embed


def build_claimed_match_embed(match_id: int, adm_mention: str) -> discord.Embed:
    row = match_row(match_id)
    players = json.loads(row["players_json"])

    embed = discord.Embed(
        title=f"Partida #{fmt_match_id(match_id)}",
        color=0x2ECC71,
        description=(
            f"🎮 **Modo:** {row['mode']}\n"
            f"👤 **Jogadores:** {mention_list_from_ids(players)}\n"
            f"✅ **Assumida por:** {adm_mention}\n"
            f"📌 **Status:** 🟢 Em andamento"
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


async def update_pending_message_as_claimed(guild: discord.Guild, match_id: int, adm: discord.Member):
    row = match_row(match_id)
    if not row or not row["pending_message_id"]:
        return

    pending_channel = guild.get_channel(CANAL_PARTIDAS_PENDENTES_ID)
    if not isinstance(pending_channel, discord.TextChannel):
        return

    try:
        msg = await pending_channel.fetch_message(row["pending_message_id"])
        await msg.edit(embed=build_claimed_match_embed(match_id, adm.mention), view=None)
    except discord.NotFound:
        pass


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
            await interaction.response.send_message("Já existe uma partida desse painel em andamento.", ephemeral=True)
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
            await create_pending_match(interaction.guild, self.panel_id)

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

        await interaction.response.defer()
        await assume_match(interaction.guild, interaction.user, self.match_id)


class MatchControlView(discord.ui.View):
    def __init__(self, match_id: int):
        super().__init__(timeout=None)
        self.match_id = match_id

        start_btn = discord.ui.Button(
            label="Iniciar",
            style=discord.ButtonStyle.success,
            custom_id=f"start_match_{match_id}"
        )
        finish_btn = discord.ui.Button(
            label="Finalizar",
            style=discord.ButtonStyle.secondary,
            custom_id=f"finish_match_{match_id}"
        )

        start_btn.callback = self.start_callback
        finish_btn.callback = self.finish_callback

        self.add_item(start_btn)
        self.add_item(finish_btn)

    async def start_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not is_adm_member(interaction.user):
            await interaction.response.send_message("Apenas ADMs podem usar esse botão.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal inválido.", ephemeral=True)
            return

        await channel.edit(name=f"em-andamento-{fmt_match_id(self.match_id)}")

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'in_progress' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        await send_staff_log(interaction.guild, f"▶️ Partida #{fmt_match_id(self.match_id)} iniciada por {interaction.user.mention}")
        await interaction.response.defer()

    async def finish_callback(self, interaction: discord.Interaction):
        row = match_row(self.match_id)
        if not row:
            await interaction.response.send_message("Partida não encontrada.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member) or not is_adm_member(interaction.user):
            await interaction.response.send_message("Apenas ADMs podem usar esse botão.", ephemeral=True)
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Canal inválido.", ephemeral=True)
            return

        final_cat = find_category(interaction.guild, NOME_CATEGORIA_FINALIZADAS)
        if final_cat:
            await channel.edit(name=f"finalizada-{fmt_match_id(self.match_id)}", category=final_cat)
        else:
            await channel.edit(name=f"finalizada-{fmt_match_id(self.match_id)}")

        with closing(db_connect()) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE matches SET status = 'finished' WHERE match_id = ?", (self.match_id,))
            conn.commit()

        await send_staff_log(interaction.guild, f"✅ Partida #{fmt_match_id(self.match_id)} finalizada por {interaction.user.mention}")
        await update_pending_message_status(interaction.guild, self.match_id, "🔴 Finalizada")
        await interaction.response.defer()


# =========================
# MATCH FLOW
# =========================
async def create_pending_match(guild: discord.Guild, panel_id: str):
    panel = panel_row(panel_id)
    if not panel:
        return

    players = panel_players(panel_id)
    if len(players) < panel["max_players"]:
        return

    pending_channel = guild.get_channel(CANAL_PARTIDAS_PENDENTES_ID)
    if not isinstance(pending_channel, discord.TextChannel):
        return

    match_id = next_match_id()

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO matches (
                match_id, panel_id, guild_id, title, mode, info,
                players_json, status, pending_message_id, claimed_by,
                private_channel_id, control_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, NULL)
        """, (
            match_id,
            panel_id,
            guild.id,
            panel["title"],
            panel["mode"],
            panel["info"],
            json.dumps(players)
        ))
        conn.commit()

    msg = await pending_channel.send(
        embed=build_pending_match_embed(match_id),
        view=ClaimMatchView(match_id)
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE matches SET pending_message_id = ? WHERE match_id = ?", (msg.id, match_id))
        cur.execute("DELETE FROM panel_players WHERE panel_id = ?", (panel_id,))
        conn.commit()

    bot.add_view(ClaimMatchView(match_id), message_id=msg.id)
    await refresh_panel_message(panel_id)
    await send_staff_log(guild, f"📥 Partida #{fmt_match_id(match_id)} criada em pendentes.")


async def assume_match(guild: discord.Guild, adm: discord.Member, match_id: int):
    row = match_row(match_id)
    if not row or row["status"] != "pending":
        return

    players = json.loads(row["players_json"])
    em_andamento_cat = find_category(guild, NOME_CATEGORIA_EM_ANDAMENTO)
    adm_role = find_role(guild, NOME_CARGO_ADM)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True
        )
    }

    if adm_role:
        overwrites[adm_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True
        )

    overwrites[adm] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True
    )

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

    private_channel = await guild.create_text_channel(
        name=f"fila-{fmt_match_id(match_id)}",
        category=em_andamento_cat,
        overwrites=overwrites,
        reason="Partida assumida por ADM"
    )

    control_msg = await private_channel.send(
        f"🎮 **Partida criada**\n"
        f"ADM responsável: {adm.mention}\n"
        f"Jogadores: {mention_list_from_ids(players)}\n\n"
        f"Use os botões abaixo para atualizar o status.",
        view=MatchControlView(match_id)
    )

    with closing(db_connect()) as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE matches
            SET status = 'claimed',
                claimed_by = ?,
                private_channel_id = ?,
                control_message_id = ?
            WHERE match_id = ?
        """, (adm.id, private_channel.id, control_msg.id, match_id))
        conn.commit()

    bot.add_view(MatchControlView(match_id), message_id=control_msg.id)
    await update_pending_message_as_claimed(guild, match_id, adm)
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
        f"🏷️ **{ROTULO_INFO}:**\n{info}\n\n"
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
    await msg.edit(view=view)
    bot.add_view(view, message_id=msg.id)


# =========================
# READY
# =========================
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

        cur.execute("SELECT match_id, pending_message_id FROM matches WHERE status = 'pending' AND pending_message_id IS NOT NULL")
        for row in cur.fetchall():
            try:
                bot.add_view(ClaimMatchView(row["match_id"]), message_id=row["pending_message_id"])
            except Exception:
                pass

        cur.execute("SELECT match_id, control_message_id FROM matches WHERE status IN ('claimed', 'in_progress', 'finished') AND control_message_id IS NOT NULL")
        for row in cur.fetchall():
            try:
                bot.add_view(MatchControlView(row["match_id"]), message_id=row["control_message_id"])
            except Exception:
                pass

    print(f"Bot ligado como {bot.user}")


db_init()
bot.run(os.getenv("DISCORD_TOKEN"))


bot.run(os.getenv("DISCORD_TOKEN"))
