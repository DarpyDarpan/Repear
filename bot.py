import discord
from discord.ext import commands
import aiohttp
import requests
import qrcode
import os
from dotenv import load_dotenv
import logging
import asyncio

# Setup logging
logging.basicConfig(level=logging.INFO)

load_dotenv()

TOKEN = os.getenv('DISCORD_BOT_TOKEN')
BLOCKCYPHER_API_TOKEN = os.getenv('BLOCKCYPHER_API_TOKEN')
SERVER_ID = int(os.getenv('DISCORD_SERVER_ID'))
BETA_ROLE_ID = int(os.getenv('DISCORD_BETA_ROLE_ID'))
TICKET_CATEGORY_ID = int(os.getenv('DISCORD_TICKET_CATEGORY_ID'))
YOUR_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
YOUR_PROFILE_PICTURE_URL = os.getenv('PROFILE_PICTURE_URL')
YOUR_LTC_ADDRESS = os.getenv('YOUR_LTC_ADDRESS')
PRIVATE_KEY = os.getenv('PRIVATE_KEY')

LTC_PRICE_USD = 0.1
CONFIRMATIONS_REQUIRED = 1
EMBED_COLOR = 0x9904D0

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

ANIMATED_EMOJI = '<a:animated_emoji:123456789012345678>'  # Replace with your actual emoji ID

class BuyButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Buy", style=discord.ButtonStyle.primary, custom_id="buy_button")
    async def buy_button_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        member = interaction.user

        # Create a new private channel for the ticket
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True)
        }
        category = guild.get_channel(TICKET_CATEGORY_ID)
        if category is None:
            logging.error("Ticket category not found!")
            return
        ticket_channel = await guild.create_text_channel(name=f'ticket-{member.name}', overwrites=overwrites, category=category)

        # Generate a new Litecoin address for this transaction
        ltc_address, private_key = await generate_new_ltc_address()
        if not ltc_address:
            await ticket_channel.send(content="Error generating Litecoin address. Please try again later.")
            return

        # Save the private key to a file
        save_private_key(ltc_address, private_key)

        # Calculate the LTC amount
        ltc_price_usd = await get_ltc_usd_price()
        if ltc_price_usd == 0:
            await ticket_channel.send(content="Error fetching Litecoin price. Please try again later.")
            return

        ltc_amount = LTC_PRICE_USD / ltc_price_usd
        qr_code_image = generate_qr_code(ltc_address, ltc_amount)
        qr_code_path = f'{ltc_address}.png'  # Save QR code with a unique name
        qr_code_image.save(qr_code_path)

        embed = discord.Embed(
            title="Purchase Beta Role",
            description="To purchase the beta role, please send the required amount in Litecoin to the provided address. Use the buttons below for easy access.",
            color=EMBED_COLOR
        )
        embed.add_field(name="Litecoin Address", value=f'`{ltc_address}`', inline=False)
        embed.add_field(name="LTC Amount", value=f'`{ltc_amount:.8f}`', inline=False)
        embed.add_field(name="USD Amount", value=f'`${LTC_PRICE_USD:.2f}`', inline=False)
        embed.set_footer(text="Bot made by TechnOh!", icon_url=YOUR_PROFILE_PICTURE_URL)
        
        buttons = PaymentButtons(ltc_address, ltc_amount, qr_code_path)
        await ticket_channel.send(content=member.mention, embed=embed, view=buttons)

        # Register the webhook for this transaction
        await register_webhook(ltc_address, ticket_channel.id, member.id)

class PaymentButtons(discord.ui.View):
    def __init__(self, ltc_address, ltc_amount, qr_code_path):
        super().__init__(timeout=None)
        self.ltc_address = ltc_address
        self.ltc_amount = ltc_amount
        self.qr_code_path = qr_code_path

    @discord.ui.button(label="Paste Payment Details", style=discord.ButtonStyle.primary, custom_id="paste_payment_details")
    async def paste_payment_details(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(content=f'Litecoin Address: `{self.ltc_address}`\nLTC Amount: `{self.ltc_amount:.8f}`', ephemeral=True)

    @discord.ui.button(label="Show QR Code", style=discord.ButtonStyle.secondary, custom_id="show_qr_code")
    async def show_qr_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(file=discord.File(self.qr_code_path), ephemeral=True)

async def sweep_ltc_to_your_address(from_address, to_address, amount, ticket_channel, wait_message_id, member):
    try:
        # Sweep LTC from `from_address` to `to_address`
        final_tx = await sweep_ltc_address(from_address, to_address, amount)
        if final_tx:
            logging.info(f"LTC successfully swept to your address: {final_tx}")

            # Notify the bot host about the transaction (e.g., using logging)
            logging.info(f"Transaction details: {final_tx}")

            # Notify the user that the transaction is confirmed and waiting for confirmations
            wait_message = await ticket_channel.fetch_message(wait_message_id)
            await wait_message.edit(content=f"{ANIMATED_EMOJI} Waiting for transaction to meet the minimum number of confirmations required...")

            # Optionally, wait for the required confirmations here (implement your own logic)
            await wait_for_confirmations(final_tx['tx']['hash'], ticket_channel, wait_message_id, member)
        else:
            logging.error("Failed to sweep LTC to your address.")
            await ticket_channel.send(content="Failed to sweep LTC to your address. Please try again.")
    except Exception as e:
        logging.error(f'Error sweeping LTC to your address: {e}')
        await ticket_channel.send(content=f'Error sweeping LTC to your address: {e}')

async def wait_for_confirmations(tx_hash, ticket_channel, wait_message_id, member):
    # Implement your logic to wait for the required number of confirmations
    # This is a placeholder implementation, you need to adjust it according to your needs
    confirmations = 0
    while confirmations < CONFIRMATIONS_REQUIRED:
        await asyncio.sleep(60)  # Check every 60 seconds
        confirmations = await get_confirmations(tx_hash)
        wait_message = await ticket_channel.fetch_message(wait_message_id)
        await wait_message.edit(content=f"{ANIMATED_EMOJI} Waiting for transaction to meet the minimum number of confirmations required... ({confirmations}/{CONFIRMATIONS_REQUIRED})")

    # Once the required confirmations are met
    wait_message = await ticket_channel.fetch_message(wait_message_id)
    await wait_message.edit(content="Transaction confirmed and required confirmations met!")

    # Assign the role to the user
    role = discord.utils.get(ticket_channel.guild.roles, id=BETA_ROLE_ID)
    if role:
        await member.add_roles(role)
        logging.info(f"Assigned {role.name} role to {member.name}")

        # Send an embed message to the ticket channel
        embed = discord.Embed(
            title="Role Assigned",
            description=f"Congratulations {member.mention}, you have been given the **{role.name}** role!",
            color=discord.Color.green()
        )
        await ticket_channel.send(embed=embed)

async def get_confirmations(tx_hash):
    # Implement your logic to get the number of confirmations for the given transaction hash
    # This is a placeholder implementation, you need to adjust it according to your needs
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://api.blockcypher.com/v1/ltc/main/txs/{tx_hash}') as response:
            response.raise_for_status()
            data = await response.json()
            return data['confirmations']

async def register_webhook(ltc_address, channel_id, user_id):
    webhook_url = "http://45.133.74.37:5000/webhook"

    payload = {
        "event": "confirmed-tx",
        "address": ltc_address,
        "url": webhook_url,
        "confirmations": CONFIRMATIONS_REQUIRED
    }

    response = requests.post(
        f'https://api.blockcypher.com/v1/ltc/main/hooks?token={BLOCKCYPHER_API_TOKEN}',
        json=payload
    )

    if response.status_code == 201:
        logging.info(f"Webhook registered successfully for {ltc_address}")
    else:
        logging.error(f"Failed to register webhook for {ltc_address}: {response.status_code}, {response.text}")

def save_private_key(ltc_address, private_key):
    with open(f'{ltc_address}_private_key.txt', 'w') as f:
        f.write(private_key)

async def generate_new_ltc_address():
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f'https://api.blockcypher.com/v1/ltc/main/addrs?token={BLOCKCYPHER_API_TOKEN}'
        ) as response:
            if response.status != 201:
                logging.error(f"Failed to generate new Litecoin address: {response.status}, {await response.text()}")
                return None, None
            data = await response.json()
            return data['address'], data['private']

async def get_ltc_usd_price():
    async with aiohttp.ClientSession() as session:
        async with session.get('https://api.coinbase.com/v2/prices/LTC-USD/spot') as response:
            if response.status != 200:
                logging.error(f"Failed to fetch Litecoin price: {response.status}, {await response.text()}")
                return 0
            data = await response.json()
            return float(data['data']['amount'])

def generate_qr_code(address, amount):
    qr = qrcode.make(f'ltc:{address}?amount={amount:.8f}')
    return qr

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    channel = bot.get_channel(YOUR_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title="Buy Beta Role",
            description="Click the button below to purchase the beta role.",
            color=EMBED_COLOR
        )
        await channel.send(embed=embed, view=BuyButton())

bot.run(TOKEN)
