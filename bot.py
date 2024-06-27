import discord
from discord.ext import commands, tasks
import aiohttp
import qrcode
import os
from dotenv import load_dotenv
import logging
import asyncio
import json

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

        # Start the transaction confirmation task
        bot.loop.create_task(wait_for_confirmations(ltc_address, ticket_channel, member, ltc_amount))

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

async def wait_for_confirmations(ltc_address, ticket_channel, member, ltc_amount):
    tx_hash = None
    sent_wait_message = False
    wait_message = None

    while not tx_hash:
        tx_hash = await get_tx_hash(ltc_address)
        if not tx_hash:
            await asyncio.sleep(5)  # Check every 5 seconds
            if not sent_wait_message:
                wait_message = await ticket_channel.send(content=f"{ANIMATED_EMOJI} Waiting for transaction to meet the minimum number of confirmations required... (0/{CONFIRMATIONS_REQUIRED})")
                sent_wait_message = True

    confirmations = 0
    while confirmations < CONFIRMATIONS_REQUIRED:
        await asyncio.sleep(5)  # Check every 5 seconds
        confirmations = await get_confirmations(tx_hash)
        if wait_message:
            await wait_message.edit(content=f"{ANIMATED_EMOJI} Waiting for transaction to meet the minimum number of confirmations required... ({confirmations}/{CONFIRMATIONS_REQUIRED})")

    if wait_message:
        await wait_message.edit(content="Transaction confirmed and required confirmations met!")

    # Send the Litecoin to the user's address
    send_success = await send_ltc(ltc_address, YOUR_LTC_ADDRESS, ltc_amount)
    if send_success:
        await ticket_channel.send(content=f"The Litecoin has been successfully sent to the address: `{YOUR_LTC_ADDRESS}`")

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
    else:
        logging.error(f"Role with ID {BETA_ROLE_ID} not found.")

async def get_tx_hash(address):
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://api.blockcypher.com/v1/ltc/main/addrs/{address}/full?token={BLOCKCYPHER_API_TOKEN}') as response:
            if response.status != 200:
                logging.error(f"Failed to fetch transactions: {response.status}")
                return None
            data = await response.json()
            if 'txs' in data and len(data['txs']) > 0:
                return data['txs'][0]['hash']
            return None

async def get_confirmations(tx_hash):
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://api.blockcypher.com/v1/ltc/main/txs/{tx_hash}') as response:
            if response.status != 200:
                logging.error(f"Failed to fetch transaction details: {response.status}")
                return 0
            data = await response.json()
            return data.get('confirmations', 0)

async def send_ltc(from_address, to_address, amount):
    try:
        async with aiohttp.ClientSession() as session:
            # Get the recommended transaction fee
            async with session.get(f'https://api.blockcypher.com/v1/ltc/main') as response:
                if response.status != 200:
                    logging.error(f"Failed to fetch Litecoin transaction fee: {response.status}")
                    return False
                data = await response.json()
                recommended_fee_per_kb = data['high_fee_per_kb']  # You can also use 'medium_fee_per_kb' or 'low_fee_per_kb'
                recommended_fee = recommended_fee_per_kb / 1024  # Convert to fee per byte

            # Get the balance of the from_address
            async with session.get(f'https://api.blockcypher.com/v1/ltc/main/addrs/{from_address}/balance') as response:
                if response.status != 200:
                    logging.error(f"Failed to fetch address balance: {response.status}")
                    return False
                data = await response.json()
                balance = data['balance']

            # Calculate the amount to send after deducting the fee
            amount_to_send = int(balance - recommended_fee)

            if amount_to_send <= 0:
                logging.error("Insufficient balance to cover the transaction fee")
                return False

            # Create the transaction skeleton
            async with session.post(
                f'https://api.blockcypher.com/v1/ltc/main/txs/new?token={BLOCKCYPHER_API_TOKEN}',
                json={
                    "inputs": [{"addresses": [from_address]}],
                    "outputs": [{"addresses": [to_address], "value": amount_to_send}]
                }
            ) as response:
                if response.status != 201:
                    logging.error(f"Failed to create transaction skeleton: {response.status}, {await response.text()}")
                    return False
                tx_skeleton = await response.json()

            # Sign the transaction
            tx_skeleton["tosign"] = [PRIVATE_KEY]  # Replace with actual signing method

            # Broadcast the transaction
            async with session.post(
                f'https://api.blockcypher.com/v1/ltc/main/txs/send?token={BLOCKCYPHER_API_TOKEN}',
                json=tx_skeleton
            ) as response:
                if response.status != 201:
                    logging.error(f"Failed to broadcast transaction: {response.status}, {await response.text()}")
                    return False

        return True
    except Exception as e:
        logging.error(f"Error sending Litecoin: {str(e)}")
        return False

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
