import discord
from discord.ext import commands
import requests
import qrcode
import asyncio
import os
from dotenv import load_dotenv
import aiohttp

# Load environment variables from .env file
load_dotenv()

TOKEN = os.getenv('DISCORD_BOT_TOKEN')
BLOCKCYPHER_API_TOKEN = os.getenv('BLOCKCYPHER_API_TOKEN')
SERVER_ID = int(os.getenv('DISCORD_SERVER_ID'))
BETA_ROLE_ID = int(os.getenv('DISCORD_BETA_ROLE_ID'))
TICKET_CATEGORY_ID = int(os.getenv('DISCORD_TICKET_CATEGORY_ID'))
YOUR_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))
YOUR_PROFILE_PICTURE_URL = os.getenv('PROFILE_PICTURE_URL')

LTC_PRICE_USD = 30.0
CONFIRMATIONS_REQUIRED = 1
EMBED_COLOR = 0x9904D0

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

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
        ticket_channel = await guild.create_text_channel(name=f'ticket-{member.name}', overwrites=overwrites, category=category)

        # Generate a new Litecoin address for this transaction
        ltc_address = await generate_new_ltc_address()
        if not ltc_address:
            await ticket_channel.send(content="Error generating Litecoin address. Please try again later.")
            return

        # Calculate the LTC amount
        ltc_price_usd = await get_ltc_usd_price()
        if ltc_price_usd == 0:
            await ticket_channel.send(content="Error fetching Litecoin price. Please try again later.")
            return

        ltc_amount = LTC_PRICE_USD / ltc_price_usd
        qr_code_image = generate_qr_code(ltc_address, ltc_amount)
        qr_code_path = 'ltc_qr.png'
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

        await asyncio.sleep(10)  # Short delay before starting the payment check
        payment_success = await check_litecoin_payment(ltc_address, ltc_amount)

        if payment_success:
            role = guild.get_role(BETA_ROLE_ID)
            await member.add_roles(role)
            embed = discord.Embed(
                title="Beta Role Granted",
                description='Congratulations! You have received the beta role.',
                color=EMBED_COLOR
            )
            embed.set_footer(text="Bot made by TechnOh!", icon_url=YOUR_PROFILE_PICTURE_URL)
            await ticket_channel.send(embed=embed)
        else:
            embed = discord.Embed(
                title="Payment Not Detected",
                description='Payment not detected. Please try again later or contact support.',
                color=EMBED_COLOR
            )
            embed.set_footer(text="Bot made by TechnOh!", icon_url=YOUR_PROFILE_PICTURE_URL)
            await ticket_channel.send(embed=embed)

        # Cleanup the QR code image file
        if os.path.exists(qr_code_path):
            os.remove(qr_code_path)

class PaymentButtons(discord.ui.View):
    def __init__(self, ltc_address, ltc_amount, qr_code_path):
        super().__init__()
        self.ltc_address = ltc_address
        self.ltc_amount = ltc_amount
        self.qr_code_path = qr_code_path

    @discord.ui.button(label="Copy Address & Amount", style=discord.ButtonStyle.secondary)
    async def copy_details_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"`{self.ltc_address}`\n`{self.ltc_amount:.8f}`", ephemeral=True)

    @discord.ui.button(label="Get QR Code", style=discord.ButtonStyle.secondary)
    async def get_qr_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(file=discord.File(self.qr_code_path), ephemeral=True)

@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')
    
    # Send the embed message with the buy button in a specific channel
    channel = bot.get_channel(YOUR_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title="Buy Beta Role",
            description="Click the button below to purchase the beta role using Litecoin.",
            color=EMBED_COLOR
        )
        embed.set_footer(text="Bot made by TechnOh!", icon_url=YOUR_PROFILE_PICTURE_URL)
        await channel.send(embed=embed, view=BuyButton())
    else:
        print(f"Channel with ID {YOUR_CHANNEL_ID} not found.")

async def check_litecoin_payment(ltc_address, ltc_amount_required):
    try:
        api_url = f'https://api.blockcypher.com/v1/ltc/main/addrs/{ltc_address}/full?token={BLOCKCYPHER_API_TOKEN}'
        
        for _ in range(10):  # Check for payment 10 times with a 1-minute interval
            response = await bot.loop.run_in_executor(None, requests.get, api_url)
            response.raise_for_status()
            data = response.json()

            print(f'API Response: {data}')  # Debugging line to check API response structure

            for tx in data.get('txs', []):
                if tx.get('confirmations', 0) >= CONFIRMATIONS_REQUIRED:
                    for output in tx.get('outputs', []):
                        if ltc_address in output.get('addresses', []) and output.get('value', 0) > 0:
                            ltc_amount = output['value'] / 1e8  # Convert from satoshis to LTC
                            usd_amount = ltc_amount * await get_ltc_usd_price()
                            
                            if usd_amount >= LTC_PRICE_USD:
                                return True
            
            await asyncio.sleep(60)  # Wait for 1 minute before checking again

        return False  # Return false if payment is not detected within the checks

    except Exception as e:
        print(f'Error processing Litecoin payment: {e}')
        return False

async def get_ltc_usd_price():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get('https://api.coingecko.com/api/v3/simple/price?ids=litecoin&vs_currencies=usd') as response:
                response.raise_for_status()
                data = await response.json()
                return data['litecoin']['usd']
    except Exception as e:
        print(f'Error fetching Litecoin price: {e}')
        return 0

async def generate_new_ltc_address():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'https://api.blockcypher.com/v1/ltc/main/addrs?token={BLOCKCYPHER_API_TOKEN}'
            ) as response:
                response.raise_for_status()
                data = await response.json()
                return data['address']
    except Exception as e:
        print(f'Error generating new Litecoin address: {e}')
        return None

def generate_qr_code(address, amount):
    qr_data = f'litecoin:{address}?amount={amount}'
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)

    img = qr.make_image(fill='black', back_color='white')
    return img

async def loading_animation(ticket_channel):
    messages = ["⏳ Loading...", "⌛ Loading...", "⏳ Loading..."]
    for i in range(10):
        await ticket_channel.send(messages[i % len(messages)])
        await asyncio.sleep(6)

bot.run(TOKEN)
