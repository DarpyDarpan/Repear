import discord
from discord.ext import commands, tasks
import aiohttp
import qrcode
import os
from dotenv import load_dotenv
import logging
import asyncio
import json
from litecoinutils.keys import P2pkhAddress, PrivateKey
from litecoinutils.setup import setup

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

    role = discord.utils.get(member.guild.roles, id=BETA_ROLE_ID)
    if role:
        await member.add_roles(role)
        await ticket_channel.send(content=f"{member.mention} has been given the beta role!")

async def generate_new_ltc_address():
    try:
        setup('mainnet')
        priv_key = PrivateKey()
        address = priv_key.get_public_key().get_address()
        ltc_address = address.to_string()
        private_key = priv_key.to_wif()
        return ltc_address, private_key
    except Exception as e:
        logging.error(f"Error generating new Litecoin address: {str(e)}")
        return None, None

def save_private_key(ltc_address, private_key):
    try:
        with open('private_keys.json', 'r') as f:
            private_keys = json.load(f)
    except FileNotFoundError:
        private_keys = {}

    private_keys[ltc_address] = private_key

    with open('private_keys.json', 'w') as f:
        json.dump(private_keys, f)

async def get_tx_hash(ltc_address):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.blockcypher.com/v1/ltc/main/addrs/{ltc_address}/full') as response:
                if response.status != 200:
                    logging.error(f"Failed to fetch transaction data: {response.status}")
                    return None
                data = await response.json()
                txs = data['txs']
                if not txs:
                    return None
                return txs[0]['hash']
    except Exception as e:
        logging.error(f"Error fetching transaction hash: {str(e)}")
        return None

async def get_confirmations(tx_hash):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.blockcypher.com/v1/ltc/main/txs/{tx_hash}') as response:
                if response.status != 200:
                    logging.error(f"Failed to fetch transaction confirmations: {response.status}")
                    return 0
                data = await response.json()
                return data['confirmations']
    except Exception as e:
        logging.error(f"Error fetching confirmations: {str(e)}")
        return 0

async def get_ltc_usd_price():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd') as response:
                if response.status != 200:
                    logging.error(f"Failed to fetch Litecoin price: {response.status}")
                    return 0
                data = await response.json()
                return data['litecoin']['usd']
    except Exception as e:
        logging.error(f"Error fetching Litecoin price: {str(e)}")
        return 0

def generate_qr_code(ltc_address, ltc_amount):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(f'litecoin:{ltc_address}?amount={ltc_amount:.8f}')
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    return img

async def send_ltc(from_address, to_address, amount):
    try:
        key = PrivateKey(PRIVATE_KEY)
        # Check the balance of the from_address
        balance = await get_address_balance(from_address)
        # Assume a fee (in satoshis, since fees are usually small, this example assumes a small fee)
        fee = 1000  # This is an example fee, you'll need to adjust based on network conditions

        if balance < amount + fee:
            logging.error(f"Not enough funds in {from_address}. Balance: {balance}, required: {amount + fee}")
            return False

        # Adjust the amount to be sent to account for the fee
        amount_to_send = amount - fee / 1e8  # Convert fee to LTC

        tx = key.create_transaction([(to_address, amount_to_send, 'ltc')], fee='low')
        NetworkAPI.broadcast_tx(tx)
        return True
    except Exception as e:
        logging.error(f"Error sending Litecoin: {str(e)}")
        return False

async def get_address_balance(address):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance') as response:
                if response.status != 200:
                    logging.error(f"Failed to fetch address balance: {response.status}")
                    return 0
                data = await response.json()
                return data['final_balance']
    except Exception as e:
        logging.error(f"Error fetching address balance: {str(e)}")
        return 0

@bot.event
async def on_ready():
    logging.info(f'We have logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send('Pong!')

@bot.command()
async def start(ctx):
    await ctx.send(content="Click the button below to buy the beta role!", view=BuyButton())

bot.run(TOKEN)
