import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

paineis = {}
partidas_ativas = {}

IMAGEM_PADRAO = "https://i.imgur.com/aUCWSvM.png"

# COLE AQUI O ID DO CANAL STAFF
CANAL_STAFF_ID = 1484323837557477508

# NOME DO CARGO E DA CATEGORIA
NOME_CARGO_ADM = "ADM"
NOME_CATEGORIA_PARTIDAS = "PARTIDAS"


class FilaView(discord.ui.View):
    def __init__(self, painel_id: str):
        super().__init__(timeout=None)
        self.painel_id = painel_id

    @discord.ui.button(
        label="Entrar na fila",
        style=discord.ButtonStyle.success,
        custom_id="botao_entrar_fila_multi"
    )
    async def entrar(self, interaction: discord.Interaction, button: discord.ui.Button):
        painel = paineis.get(self.painel_id)

        if not painel:
            await interaction.response.send_message("Esse painel não foi encontrado.", ephemeral=True)
            return

        user_id = str(interaction.user.id)

        if user_id in painel["jogadores"]:
            await interaction.response.send_message("Você já está nessa fila.", ephemeral=True)
            return

        if len(painel["jogadores"]) >= painel["max"]:
            await interaction.response.send_message("Fila cheia.", ephemeral=True)
            return

        painel["jogadores"].append(user_id)
        await atualizar_painel(interaction.channel, self.painel_id)
        await interaction.response.send_message("Você entrou na fila.", ephemeral=True)

        if len(painel["jogadores"]) == painel["max"]:
            await formar_partida(interaction.guild, interaction.channel, self.painel_id)

    @discord.ui.button(
        label="Sair da fila",
        style=discord.ButtonStyle.danger,
        emoji="🔻",
        custom_id="botao_sair_fila_multi"
    )
    async def sair(self, interaction: discord.Interaction, button: discord.ui.Button):
        painel = paineis.get(self.painel_id)

        if not painel:
            await interaction.response.send_message("Esse painel não foi encontrado.", ephemeral=True)
            return

        user_id = str(interaction.user.id)

        if user_id not in painel["jogadores"]:
            await interaction.response.send_message("Você não está nessa fila.", ephemeral=True)
            return

        painel["jogadores"].remove(user_id)
        await atualizar_painel(interaction.channel, self.painel_id)
        await interaction.response.send_message("Você saiu da fila.", ephemeral=True)


def criar_embed(painel_id: str):
    painel = paineis[painel_id]

    if painel["jogadores"]:
        lista = "\n".join([f"<@{uid}>" for uid in painel["jogadores"]])
    else:
        lista = "Nenhum jogador na fila"

    embed = discord.Embed(
        title=painel["titulo"],
        color=0x2ECC71
    )

    embed.description = (
        f"🎮 **Modo:**\n"
        f"{painel['modo']}\n\n"
        f"💸 **Valor:**\n"
        f"{painel['info']}\n\n"
        f"👤 **Jogadores:**\n"
        f"{lista}"
    )

    if painel["imagem"]:
        embed.set_thumbnail(url=painel["imagem"])

    return embed


async def atualizar_painel(canal, painel_id: str):
    async for msg in canal.history(limit=100):
        if str(msg.id) == painel_id:
            await msg.edit(embed=criar_embed(painel_id), view=FilaView(painel_id))
            return


def definir_max_jogadores(modo: str):
    modo_limpo = modo.lower().strip()

    if modo_limpo in ["1x1", "1v1"]:
        return 2
    elif modo_limpo in ["2x2", "2v2"]:
        return 4
    elif modo_limpo in ["3x3", "3v3"]:
        return 6
    elif modo_limpo in ["4x4", "4v4"]:
        return 8
    else:
        return 2


def encontrar_cargo_adm(guild: discord.Guild):
    for cargo in guild.roles:
        if cargo.name.lower() == NOME_CARGO_ADM.lower():
            return cargo
    return None


def encontrar_categoria_partidas(guild: discord.Guild):
    for categoria in guild.categories:
        if categoria.name.lower() == NOME_CATEGORIA_PARTIDAS.lower():
            return categoria
    return None


def sanitizar_nome(texto: str):
    return (
        texto.lower()
        .replace(" ", "-")
        .replace("|", "")
        .replace(":", "")
        .replace("/", "-")
    )


async def formar_partida(guild: discord.Guild, canal_painel: discord.TextChannel, painel_id: str):
    painel = paineis.get(painel_id)
    if not painel:
        return

    jogadores_ids = painel["jogadores"][:]
    if not jogadores_ids:
        return

    cargo_adm = encontrar_cargo_adm(guild)
    categoria = encontrar_categoria_partidas(guild)
    canal_staff = guild.get_channel(CANAL_STAFF_ID)

    nomes_base = "-vs-".join([uid[-4:] for uid in jogadores_ids[:2]])
    nome_canal = sanitizar_nome(f"partida-{painel['modo']}-{nomes_base}")[:90]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True
        )
    }

    if cargo_adm:
        overwrites[cargo_adm] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True
        )

    mentions_jogadores = []
    for uid in jogadores_ids:
        membro = guild.get_member(int(uid))
        if membro:
            overwrites[membro] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )
            mentions_jogadores.append(membro.mention)
        else:
            mentions_jogadores.append(f"<@{uid}>")

    if categoria:
        canal_partida = await guild.create_text_channel(
            name=nome_canal,
            category=categoria,
            overwrites=overwrites,
            reason="Partida criada automaticamente pelo bot"
        )
    else:
        canal_partida = await guild.create_text_channel(
            name=nome_canal,
            overwrites=overwrites,
            reason="Partida criada automaticamente pelo bot"
        )

    partidas_ativas[str(canal_partida.id)] = {
        "painel_id": painel_id,
        "titulo": painel["titulo"],
        "modo": painel["modo"],
        "jogadores": jogadores_ids
    }

    texto_jogadores = " vs ".join(mentions_jogadores)

    if canal_staff:
        await canal_staff.send(
            f"🔥 **Partida criada**\n"
            f"Painel: **{painel['titulo']}**\n"
            f"Jogadores: {texto_jogadores}\n"
            f"Canal: {canal_partida.mention}"
        )

    await canal_partida.send(
        f"🎮 **Partida criada**\n"
        f"Painel: **{painel['titulo']}**\n"
        f"Jogadores: {texto_jogadores}\n\n"
        f"Quando terminar, um ADM pode usar `!encerrarpartida` neste canal."
    )

    # limpa o painel para deixá-lo livre imediatamente
    painel["jogadores"].clear()
    await atualizar_painel(canal_painel, painel_id)


@bot.command()
async def painelz(ctx, titulo: str, modo: str, info: str):
    max_jogadores = definir_max_jogadores(modo)

    embed = discord.Embed(
        title=titulo,
        color=0x2ECC71
    )

    embed.description = (
        f"🎮 **Modo:**\n"
        f"{modo}\n\n"
       f"💸 **Valor:**\n"
        f"{info}\n\n"
        f"👤 **Jogadores:**\n"
        f"Nenhum jogador na fila"
    )

    embed.set_thumbnail(url=IMAGEM_PADRAO)

    mensagem = await ctx.send(embed=embed)

    painel_id = str(mensagem.id)
    paineis[painel_id] = {
        "titulo": titulo,
        "modo": modo,
        "info": info,
        "imagem": IMAGEM_PADRAO,
        "jogadores": [],
        "max": max_jogadores
    }

    await mensagem.edit(view=FilaView(painel_id))


@bot.command()
async def limparpainel(ctx, mensagem_id: str):
    painel = paineis.get(mensagem_id)

    if not painel:
        await ctx.send("Painel não encontrado.")
        return

    painel["jogadores"].clear()
    await atualizar_painel(ctx.channel, mensagem_id)
    await ctx.send("Fila do painel limpa.")


@bot.command()
async def mudarinfo(ctx, mensagem_id: str, *, nova_info: str):
    painel = paineis.get(mensagem_id)

    if not painel:
        await ctx.send("Painel não encontrado.")
        return

    painel["info"] = nova_info
    await atualizar_painel(ctx.channel, mensagem_id)
    await ctx.send("Informação atualizada.")


@bot.command()
async def mudartitulo(ctx, mensagem_id: str, *, novo_titulo: str):
    painel = paineis.get(mensagem_id)

    if not painel:
        await ctx.send("Painel não encontrado.")
        return

    painel["titulo"] = novo_titulo
    await atualizar_painel(ctx.channel, mensagem_id)
    await ctx.send("Título atualizado.")


@bot.command()
async def mudarfoto(ctx, mensagem_id: str, nova_url: str):
    painel = paineis.get(mensagem_id)

    if not painel:
        await ctx.send("Painel não encontrado.")
        return

    painel["imagem"] = nova_url
    await atualizar_painel(ctx.channel, mensagem_id)
    await ctx.send("Imagem atualizada.")


@bot.command()
async def encerrarpartida(ctx):
    canal_id = str(ctx.channel.id)
    partida = partidas_ativas.get(canal_id)

    if not partida:
        await ctx.send("Este canal não está registrado como partida ativa.")
        return

    await ctx.send("🗑️ Encerrando partida e apagando canal em 3 segundos...")
    del partidas_ativas[canal_id]
    await discord.utils.sleep_until(discord.utils.utcnow() + discord.utils.timedelta(seconds=3))
    await ctx.channel.delete(reason="Partida encerrada pelo bot")


@bot.event
async def on_ready():
    print(f"Bot ligado como {bot.user}")


bot.run(os.getenv("DISCORD_TOKEN"))