import os
import json
import hmac
import base64
import hashlib
import time
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import threading

app = Flask(__name__)

API_KEY = os.getenv('BITGET_API_KEY')
API_SECRET = os.getenv('BITGET_API_SECRET')
API_PASSPHRASE = os.getenv('BITGET_API_PASSPHRASE')
BASE_URL = 'https://api.bitget.com'
LEVERAGE = 2
TARGET_SYMBOL = 'WLFIUSDT'
MAX_POSITIONS_PER_SIDE = 2  # Pyramiding = 2

# Controle de posições
positions_tracker = {
    'long': {'count': 0, 'last_time': 0},
    'short': {'count': 0, 'last_time': 0}
}
tracker_lock = threading.Lock()

def log(message):
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] {message}")

def generate_signature(timestamp, method, request_path, body=''):
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
    timestamp = str(int(time.time() * 1000))
    body_str = ''
    if params and method == 'POST':
        body_str = json.dumps(params)
    
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': generate_signature(timestamp, method, endpoint, body_str if body_str else ''),
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
        return response.json()
    except Exception as e:
        log(f"❌ Erro: {e}")
        return None

def set_leverage(symbol, leverage, hold_side):
    endpoint = '/api/v2/mix/account/set-leverage'
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginCoin': 'USDT',
        'leverage': str(leverage),
        'holdSide': hold_side
    }
    result = bitget_request('POST', endpoint, params)
    return result and result.get('code') == '00000'

def get_account_balance():
    endpoint = '/api/v2/mix/account/accounts?productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint, None)
    if result and result.get('code') == '00000':
        for account in result.get('data', []):
            if account.get('marginCoin') == 'USDT':
                return float(account.get('available', 0))
    return 0.0

def get_current_price(symbol):
    endpoint = f'/api/v2/mix/market/ticker?symbol={symbol}&productType=USDT-FUTURES'
    result = bitget_request('GET', endpoint)
    if result and result.get('code') == '00000':
        data = result.get('data', [])
        price = float(data[0].get('lastPr', 0)) if isinstance(data, list) else float(data.get('lastPr', 0))
        return price if price > 0 else None
    return None

def get_positions(symbol):
    endpoint = f'/api/v2/mix/position/all-position?productType=USDT-FUTURES&marginCoin=USDT'
    result = bitget_request('GET', endpoint, None)
    
    long_total = 0.0
    short_total = 0.0
    
    if result and result.get('code') == '00000':
        for pos in result.get('data', []):
            if pos.get('symbol') == symbol:
                total = float(pos.get('total', 0))
                hold_side = pos.get('holdSide', '')
                
                if hold_side == 'long':
                    long_total += abs(total)
                elif hold_side == 'short':
                    short_total += abs(total)
    
    return {'long': long_total, 'short': short_total}

def place_order(symbol, side, trade_side, quantity):
    endpoint = '/api/v2/mix/order/place-order'
    
    params = {
        'symbol': symbol,
        'productType': 'USDT-FUTURES',
        'marginMode': 'crossed',
        'marginCoin': 'USDT',
        'size': str(quantity),
        'side': side,
        'tradeSide': trade_side,
        'orderType': 'market'
    }
    
    result = bitget_request('POST', endpoint, params)
    
    if result and result.get('code') == '00000':
        order_id = result['data'].get('orderId', 'N/A')
        log(f"✅ {side.upper()}-{trade_side.upper()} {quantity} | ID: {order_id}")
        return True
    else:
        log(f"❌ Falhou: {result}")
        return False

def can_add_position(side):
    """Verifica se pode adicionar mais uma posição"""
    with tracker_lock:
        current_time = time.time()
        
        # Reset contador se passou mais de 30 segundos
        if current_time - positions_tracker[side]['last_time'] > 30:
            positions_tracker[side]['count'] = 0
        
        if positions_tracker[side]['count'] < MAX_POSITIONS_PER_SIDE:
            positions_tracker[side]['count'] += 1
            positions_tracker[side]['last_time'] = current_time
            return True
        
        return False

def reset_position_counter(side):
    """Reseta contador quando fecha todas as posições"""
    with tracker_lock:
        positions_tracker[side]['count'] = 0

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        
        action = data.get('action', '').lower()
        market_position = data.get('marketPosition', '').lower()
        
        log(f"📨 {action.upper()}/{market_position.upper()}")
        
        symbol = TARGET_SYMBOL
        price = get_current_price(symbol)
        if not price:
            return jsonify({'status': 'error'}), 500
        
        balance = get_account_balance()
        positions = get_positions(symbol)
        
        log(f"💰 ${balance:.2f} | L:{positions['long']} S:{positions['short']}")
        
        # ABRIR LONG
        if action == 'buy' and market_position == 'long':
            log("🟢 LONG")
            
            # Verifica se pode adicionar posição
            if not can_add_position('long'):
                log("⚠️ Máximo de LONGs atingido")
                return jsonify({'status': 'ignored'}), 200
            
            # Fecha SHORTs se tiver
            if positions['short'] > 0:
                log(f"🔄 Fechando SHORT: {positions['short']}")
                place_order(symbol, 'buy', 'close', positions['short'])
                reset_position_counter('short')
                time.sleep(1)
                balance = get_account_balance()
            
            set_leverage(symbol, LEVERAGE, 'long')
            time.sleep(0.5)
            
            if balance <= 0:
                return jsonify({'status': 'error'}), 500
            
            # Usa 50% do saldo disponível para esta posição
            usable_balance = balance * 0.5
            quantity = round((usable_balance * LEVERAGE) / price, 4)
            
            log(f"📊 Usando ${usable_balance:.2f} ({quantity} WLFI)")
            
            success = place_order(symbol, 'buy', 'open', quantity)
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        # ABRIR SHORT
        elif action == 'sell' and market_position == 'short':
            log("🔴 SHORT")
            
            # Verifica se pode adicionar posição
            if not can_add_position('short'):
                log("⚠️ Máximo de SHORTs atingido")
                return jsonify({'status': 'ignored'}), 200
            
            # Fecha LONGs se tiver
            if positions['long'] > 0:
                log(f"🔄 Fechando LONG: {positions['long']}")
                place_order(symbol, 'sell', 'close', positions['long'])
                reset_position_counter('long')
                time.sleep(1)
                balance = get_account_balance()
            
            set_leverage(symbol, LEVERAGE, 'short')
            time.sleep(0.5)
            
            if balance <= 0:
                return jsonify({'status': 'error'}), 500
            
            # Usa 50% do saldo disponível para esta posição
            usable_balance = balance * 0.5
            quantity = round((usable_balance * LEVERAGE) / price, 4)
            
            log(f"📊 Usando ${usable_balance:.2f} ({quantity} WLFI)")
            
            success = place_order(symbol, 'sell', 'open', quantity)
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        # FECHAR LONG
        elif action == 'sell' and market_position == 'flat':
            log("🔵 FECHAR LONG")
            
            if positions['long'] > 0:
                success = place_order(symbol, 'sell', 'close', positions['long'])
                if success:
                    reset_position_counter('long')
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            else:
                log("⚠️ Sem LONG")
                return jsonify({'status': 'warning'}), 200
        
        # FECHAR SHORT
        elif action == 'buy' and market_position == 'flat':
            log("🔵 FECHAR SHORT")
            
            if positions['short'] > 0:
                success = place_order(symbol, 'buy', 'close', positions['short'])
                if success:
                    reset_position_counter('short')
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            else:
                log("⚠️ Sem SHORT")
                return jsonify({'status': 'warning'}), 200
        
        else:
            log(f"⏭️ Ignorado")
            return jsonify({'status': 'ignored'}), 200
    
    except Exception as e:
        log(f"❌ ERRO: {e}")
        return jsonify({'status': 'error'}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'online'}), 200

@app.route('/', methods=['GET'])
def home():
    return '''
    <h1>🤖 Bot WLFI - Pyramiding 2</h1>
    <p>Status: Online ✅</p>
    <ul>
        <li>Par: WLFIUSDT</li>
        <li>Alavancagem: 2x</li>
        <li>Pyramiding: 2 (máx 2 posições por lado)</li>
        <li>Uso de saldo: 50% por posição</li>
    </ul>
    ''', 200

def keep_alive():
    def ping():
        while True:
            try:
                time.sleep(840)
                port = int(os.getenv('PORT', 5000))
                requests.get(f"http://localhost:{port}/health", timeout=5)
                log("💓")
            except:
                pass
    threading.Thread(target=ping, daemon=True).start()

if __name__ == '__main__':
    log("🚀 Bot WLFI - Pyramiding 2")
    
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        log("❌ Credenciais faltando!")
        exit(1)
    
    keep_alive()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
