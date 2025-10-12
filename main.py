import os
import json
import hmac
import base64
import hashlib
import time
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
TARGET_SYMBOL = 'WLFIUSDT'  # Par fixo: WLFI/USDT Futures

def log(message):
    """Log com timestamp"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] {message}")

def generate_signature(timestamp, method, request_path, body=''):
    """Gera assinatura para autenticação na Bitget"""
    if body:
        body = json.dumps(body)
    message = str(timestamp) + method.upper() + request_path + body
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
    
    # Gera corpo da requisição para assinatura
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
        log(f"API Response: {json.dumps(result)}")
        return result
    except requests.exceptions.Timeout:
        log(f"❌ Timeout na requisição para {endpoint}")
        return None
    except requests.exceptions.RequestException as e:
        log(f"❌ Erro na requisição Bitget: {e}")
        if hasattr(e.response, 'text'):
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
        
        # Procura saldo USDT
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
    
    # Usa 100% do saldo com alavancagem
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
        'side': side,  # 'open_long' ou 'close_long'
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

@app.route('/webhook', methods=['POST'])
def webhook():
    """Endpoint que recebe webhooks do TradingView"""
    try:
        data = request.get_json()
        log(f"📨 Webhook recebido: {json.dumps(data)}")
        
        # Valida campos obrigatórios
        required_fields = ['action', 'marketPosition']
        if not all(field in data for field in required_fields):
            log("❌ Campos obrigatórios ausentes no JSON")
            return jsonify({'status': 'error', 'message': 'Campos obrigatórios ausentes'}), 400
        
        action = data['action']
        market_position = data['marketPosition']
        
        # SEMPRE usa WLFIUSDT
        symbol = TARGET_SYMBOL
        
        log(f"🎯 Processando: {action} | Posição: {market_position} | {symbol}")
        
        # Verifica se é operação LONG válida
        if action == 'buy' and market_position == 'long':
            log("🟢 SINAL DE COMPRA: ABRIR LONG")
            
            # Configura alavancagem 2x
            set_leverage(symbol, LEVERAGE)
            time.sleep(0.5)
            
            # Obtém preço atual de mercado
            current_price = get_current_price(symbol)
            if not current_price:
                log("❌ Não foi possível obter preço de mercado")
                return jsonify({'status': 'error', 'message': 'Erro ao obter preço'}), 500
            
            # Calcula quantidade usando 100% do saldo
            quantity = calculate_quantity_from_balance(current_price, LEVERAGE)
            
            if quantity <= 0:
                log("❌ Quantidade inválida ou sem saldo")
                return jsonify({'status': 'error', 'message': 'Sem saldo ou quantidade inválida'}), 500
            
            # Executa ordem MARKET (compra imediata)
            log(f"🚀 COMPRANDO A MERCADO: {quantity} WLFI")
            success = place_order(symbol, 'open_long', quantity)
            
            if success:
                return jsonify({
                    'status': 'success',
                    'action': 'LONG ABERTO A MERCADO',
                    'symbol': 'WLFIUSDT',
                    'quantity': quantity,
                    'price': current_price,
                    'leverage': LEVERAGE,
                    'order_type': 'MARKET'
                }), 200
            else:
                return jsonify({'status': 'error', 'message': 'Falha ao abrir LONG'}), 500
        
        elif action == 'sell' and market_position == 'flat':
            log("🔴 SINAL DE VENDA: FECHAR LONG")
            
            # Obtém tamanho da posição atual
            position_size = get_current_position(symbol)
            
            if position_size > 0:
                log(f"🚀 VENDENDO A MERCADO: {position_size} WLFI")
                success = place_order(symbol, 'close_long', position_size)
                
                if success:
                    return jsonify({
                        'status': 'success',
                        'action': 'LONG FECHADO A MERCADO',
                        'symbol': 'WLFIUSDT',
                        'quantity': position_size,
                        'order_type': 'MARKET'
                    }), 200
                else:
                    return jsonify({'status': 'error', 'message': 'Falha ao fechar LONG'}), 500
            else:
                log("⚠️ Sem posição LONG aberta para fechar")
                return jsonify({'status': 'warning', 'message': 'Sem posição aberta'}), 200
        
        else:
            log(f"⏭️ Sinal ignorado: {action} com posição {market_position}")
            return jsonify({'status': 'ignored', 'message': 'Não é operação LONG válida'}), 200
    
    except Exception as e:
        log(f"❌ ERRO CRÍTICO: {str(e)}")
        import traceback
        log(traceback.format_exc())
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Endpoint de health check"""
    return jsonify({
        'status': 'online',
        'timestamp': datetime.now().isoformat(),
        'message': 'Bot TradingView → Bitget funcionando'
    }), 200

@app.route('/', methods=['GET'])
def home():
    """Página inicial"""
    return '''
    <h1>🤖 TradingView to Bitget Bot - WLFI</h1>
    <p>Status: <strong>Online ✅</strong></p>
    <p>Webhook URL: <code>/webhook</code></p>
    <p>Health Check: <code>/health</code></p>
    <hr>
    <p><strong>Configurações:</strong></p>
    <ul>
        <li>Par: <strong>WLFIUSDT</strong></li>
        <li>Alavancagem: <strong>2x (fixo)</strong></li>
        <li>Saldo usado: <strong>100% disponível</strong></li>
        <li>Tipo de ordem: <strong>MARKET (imediata)</strong></li>
        <li>Modo: <strong>LONG apenas</strong></li>
    </ul>
    <hr>
    <p><strong>Como funciona:</strong></p>
    <ul>
        <li>✅ Recebe sinal "buy" do TradingView → <strong>COMPRA TUDO a mercado</strong></li>
        <li>✅ Recebe sinal "sell" do TradingView → <strong>VENDE TUDO a mercado</strong></li>
        <li>✅ Usa sempre 100% do saldo com 2x de alavancagem</li>
        <li>✅ Execução instantânea (market order)</li>
    </ul>
    ''', 200

def keep_alive_worker():
    """Worker thread que mantém o bot acordado fazendo auto-ping"""
    import threading
    import time
    
    def ping_self():
        while True:
            try:
                time.sleep(840)  # 14 minutos (antes dos 15min de sleep)
                port = int(os.getenv('PORT', 5000))
                url = f"http://localhost:{port}/health"
                
                # Tenta fazer ping local
                try:
                    response = requests.get(url, timeout=5)
                    if response.status_code == 200:
                        log("💓 Keep-alive: Bot mantido acordado")
                except:
                    # Se falhar localmente, não faz nada (normal no Render)
                    pass
            except Exception as e:
                log(f"⚠️ Erro no keep-alive: {e}")
    
    thread = threading.Thread(target=ping_self, daemon=True)
    thread.start()
    log("💓 Keep-alive thread iniciada (ping a cada 14min)")

if __name__ == '__main__':
    log("🚀 Iniciando bot TradingView → Bitget WLFI")
    log(f"⚙️ Par: {TARGET_SYMBOL}")
    log(f"⚙️ Alavancagem: {LEVERAGE}x FIXA")
    log(f"⚙️ Saldo: USA 100% DISPONÍVEL")
    log(f"⚙️ Tipo: MARKET ORDERS (imediatas)")
    
    # Valida credenciais
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        log("❌ ERRO: Credenciais da Bitget não configuradas!")
        log("Configure as variáveis: BITGET_API_KEY, BITGET_API_SECRET, BITGET_API_PASSPHRASE")
        exit(1)
    
    # Inicia keep-alive worker
    keep_alive_worker()
    
    # Porta para Render.com
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
