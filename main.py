import os
import json
import hmac
import base64
import hashlib
import time
import re
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# Configurações da Bitget
API_KEY = os.getenv('BITGET_API_KEY')
API_SECRET = os.getenv('BITGET_API_SECRET')
API_PASSPHRASE = os.getenv('BITGET_API_PASSPHRASE')
BASE_URL = 'https://api.bitget.com'

# Configurações do bot
LEVERAGE = 2
TARGET_SYMBOL = 'WLFIUSDT'

def log(message):
    """Log com timestamp"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")

def generate_signature(timestamp, method, request_path, body=''):
    """Gera assinatura para autenticação na Bitget"""
    if body and isinstance(body, dict):
        body = json.dumps(body)
    message = str(timestamp) + method.upper() + request_path + (body if body else '')
    mac = hmac.new(
        bytes(API_SECRET, encoding='utf8'),
        bytes(message, encoding='utf-8'),
        digestmod=hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None):
    """Faz requisição autenticada para a Bitget API"""
    timestamp = str(int(time.time() * 1000))
    request_path = endpoint
    
    body_str = ''
    if params and method == 'POST':
        body_str = json.dumps(params)
    
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': generate_signature(timestamp, method, request_path, body_str if body_str else ''),
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': API_PASSPHRASE,
        'Content-Type': 'application/json',
        'locale': 'en-US'
    }
    
    url = BASE_URL + endpoint
    
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=10)
        elif method == 'POST':
            response = requests.post(url, headers=headers, data=body_str if body_str else None, timeout=10)
        
        response.raise_for_status()
        result = response.json()
        return result
    except requests.exceptions.Timeout:
        log(f"❌ Timeout na requisição para {endpoint}")
        return None
    except requests.exceptions.RequestException as e:
        log(f"❌ Erro na requisição Bitget: {e}")
        if hasattr(e, 'response') and e.response is not None:
            log(f"Response: {e.response.text}")
        return None
    except Exception as e:
        log(f"❌ Erro inesperado: {e}")
        return None

def set_leverage(symbol, leverage):
    """Define alavancagem para o par"""
    endpoint = '/api/v2/mix/account/set-leverage'
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginCoin': 'USDT',
        'leverage': str(leverage),
        'holdSide': 'long'
    }
    
    log(f"🔧 Configurando alavancagem {leverage}x para {symbol}")
    result = bitget_request('POST', endpoint, params)
    
    if result and result.get('code') == '00000':
        log(f"✅ Alavancagem configurada: {leverage}x")
        return True
    else:
        log(f"⚠️ Aviso ao configurar alavancagem: {result}")
        return False

def get_current_price(symbol):
    """Obtém preço atual do mercado"""
    endpoint = f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint)
    
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        if isinstance(data, list) and len(data) > 0:
            price = float(data[0].get('lastPr', 0))
        else:
            price = float(data.get('lastPr', 0))
        
        if price > 0:
            log(f"💰 Preço atual {symbol}: ${price}")
            return price
    
    log(f"❌ Erro ao obter preço de {symbol}")
    return None

def get_account_balance():
    """Obtém saldo disponível em USDT na conta Futures"""
    endpoint = '/api/v2/mix/account/accounts?productType=USDT-FUTURES'
    
    result = bitget_request('GET', endpoint, None)
    
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        
        for account in data:
            if account.get('marginCoin') == 'USDT':
                available = float(account.get('available', 0))
                log(f"💰 Saldo disponível: ${available:.2f} USDT")
                return available
    
    log(f"❌ Erro ao obter saldo da conta")
    return 0.0

def calculate_quantity_from_balance(price, leverage):
    """Calcula quantidade usando 100% do saldo disponível"""
    balance = get_account_balance()
    
    if balance <= 0:
        log("❌ Sem saldo disponível!")
        return 0
    
    total_value = balance * leverage
    quantity = total_value / price
    
    log(f"📊 Saldo: ${balance:.2f} | Alavancagem: {leverage}x | Valor total: ${total_value:.2f}")
    log(f"📊 Preço: ${price:.4f} | Quantidade: {quantity:.4f}")
    
    return round(quantity, 4)

def get_current_position(symbol):
    """Obtém o tamanho da posição atual"""
    endpoint = f'/api/v2/mix/position/single-position?symbol={symbol}&productType=USDT-FUTURES&marginCoin=USDT'
    
    result = bitget_request('GET', endpoint, None)
    
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        if data and len(data) > 0:
            position = data[0]
            total = float(position.get('total', 0))
            log(f"📊 Posição atual: {total}")
            return abs(total)
    
    log("📊 Sem posição aberta")
    return 0.0

def place_order(symbol, side, quantity):
    """Executa ordem de mercado na Bitget"""
    endpoint = '/api/v2/mix/order/place-order'
    
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginMode': 'crossed',
        'marginCoin': 'USDT',
        'size': str(quantity),
        'side': side,
        'orderType': 'market',
        'force': 'gtc'
    }
    
    log(f"📤 Enviando ordem: {side} {quantity} {symbol}")
    result = bitget_request('POST', endpoint, params)
    
    if result and result.get('code') == '00000':
        order_id = result['data']['orderId']
        log(f"✅ Ordem executada! ID: {order_id}")
        return True
    else:
        log(f"❌ Erro ao executar ordem: {result}")
        return False

def parse_tradingview_message(data):
    """Interpreta mensagem do TradingView (aceita qualquer formato)"""
    
    # Se vier como string, tenta converter para dict
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except:
            # Se não for JSON, tenta extrair informações do texto
            log("⚠️ Mensagem não é JSON, tentando extrair informações...")
            
            # Procura por palavras-chave
            text = data.lower()
            
            # Detecta ação
            if 'buy' in text or 'compra' in text or 'long' in text:
                action = 'buy'
            elif 'sell' in text or 'venda' in text or 'short' in text or 'close' in text:
                action = 'sell'
            else:
                action = None
            
            # Detecta posição
            if 'long' in text and 'flat' not in text:
                market_position = 'long'
            elif 'flat' in text or 'close' in text:
                market_position = 'flat'
            else:
                market_position = None
            
            return {
                'action': action,
                'marketPosition': market_position
            }
    
    # Se já for dict, retorna como está
    if isinstance(data, dict):
        return data
    
    return {}

@app.route('/webhook', methods=['POST'])
def webhook():
    """Endpoint que recebe webhooks do TradingView"""
    try:
        # Pega dados (aceita JSON ou texto)
        if request.is_json:
            data = request.get_json()
        else:
            data = request.data.decode('utf-8')
        
        log(f"📨 Webhook recebido: {data}")
        
        # Interpreta mensagem
        parsed = parse_tradingview_message(data)
        
        action = parsed.get('action', '').lower()
        market_position = parsed.get('marketPosition', '').lower()
        
        log(f"🎯 Interpretado: action={action}, marketPosition={market_position}")
        
        symbol = TARGET_SYMBOL
        
        # COMPRAR (abrir LONG)
        if action == 'buy' and market_position == 'long':
            log("🟢 SINAL DE COMPRA: ABRIR LONG")
            
            set_leverage(symbol, LEVERAGE)
            time.sleep(0.5)
            
            current_price = get_current_price(symbol)
            if not current_price:
                return jsonify({'status': 'error', 'message': 'Erro ao obter preço'}), 500
            
            quantity = calculate_quantity_from_balance(current_price, LEVERAGE)
            
            if quantity <= 0:
                return jsonify({'status': 'error', 'message': 'Sem saldo'}), 500
            
            log(f"🚀 COMPRANDO A MERCADO: {quantity} WLFI")
            success = place_order(symbol, 'open_long', quantity)
            
            if success:
                return jsonify({
                    'status': 'success',
                    'action': 'LONG ABERTO',
                    'quantity': quantity,
                    'price': current_price
                }), 200
            else:
                return jsonify({'status': 'error', 'message': 'Falha ao abrir LONG'}), 500
        
        # VENDER (fechar LONG)
        elif action == 'sell' and market_position == 'flat':
            log("🔴 SINAL DE VENDA: FECHAR LONG")
            
            position_size = get_current_position(symbol)
            
            if position_size > 0:
                log(f"🚀 VENDENDO A MERCADO: {position_size} WLFI")
                success = place_order(symbol, 'close_long', position_size)
                
                if success:
                    return jsonify({
                        'status': 'success',
                        'action': 'LONG FECHADO',
                        'quantity': position_size
                    }), 200
                else:
                    return jsonify({'status': 'error', 'message': 'Falha ao fechar'}), 500
            else:
                log("⚠️ Sem posição para fechar")
                return jsonify({'status': 'warning', 'message': 'Sem posição'}), 200
        
        else:
            log(f"⏭️ Sinal ignorado: {action}/{market_position}")
            return jsonify({'status': 'ignored', 'message': 'Sinal não reconhecido'}), 200
    
    except Exception as e:
        log(f"❌ ERRO: {str(e)}")
        import traceback
        log(traceback.format_exc())
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check"""
    return jsonify({
        'status': 'online',
        'timestamp': datetime.now().isoformat(),
        'message': 'Bot funcionando'
    }), 200

@app.route('/', methods=['GET'])
def home():
    """Página inicial"""
    return '''
    <h1>🤖 Bot TradingView → Bitget WLFI</h1>
    <p>Status: <strong>Online ✅</strong></p>
    <p>Webhook: <code>/webhook</code></p>
    <p>Health: <code>/health</code></p>
    <hr>
    <p><strong>Configurações:</strong></p>
    <ul>
        <li>Par: <strong>WLFIUSDT</strong></li>
        <li>Alavancagem: <strong>2x</strong></li>
        <li>Saldo: <strong>100%</strong></li>
        <li>Tipo: <strong>MARKET</strong></li>
    </ul>
    ''', 200

def keep_alive_worker():
    """Mantém bot acordado"""
    import threading
    
    def ping_self():
        while True:
            try:
                time.sleep(840)
                port = int(os.getenv('PORT', 5000))
                url = f"http://localhost:{port}/health"
                try:
                    response = requests.get(url, timeout=5)
                    if response.status_code == 200:
                        log("💓 Keep-alive OK")
                except:
                    pass
            except Exception as e:
                log(f"⚠️ Keep-alive error: {e}")
    
    thread = threading.Thread(target=ping_self, daemon=True)
    thread.start()
    log("💓 Keep-alive iniciado")

if __name__ == '__main__':
    log("🚀 Iniciando Bot WLFI")
    log(f"⚙️ Par: {TARGET_SYMBOL}")
    log(f"⚙️ Alavancagem: {LEVERAGE}x")
    log(f"⚙️ Saldo: 100%")
    log(f"⚙️ Tipo: MARKET")
    
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        log("❌ Credenciais não configuradas!")
        exit(1)
    
    keep_alive_worker()
    
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
