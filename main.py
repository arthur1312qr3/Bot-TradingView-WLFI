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
POSITION_SIZE_PERCENT = 0.25  # 25% do saldo por trade

# Controle de posições
position_tracker = {
    'long': {'count': 0, 'sizes': []},
    'short': {'count': 0, 'sizes': []}
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
    
    long_positions = []
    short_positions = []
    
    if result and result.get('code') == '00000':
        for pos in result.get('data', []):
            if pos.get('symbol') == symbol:
                total = float(pos.get('total', 0))
                hold_side = pos.get('holdSide', '')
                
                if hold_side == 'long' and total > 0:
                    long_positions.append(abs(total))
                elif hold_side == 'short' and total > 0:
                    short_positions.append(abs(total))
    
    return {
        'long': {'count': len(long_positions), 'sizes': long_positions},
        'short': {'count': len(short_positions), 'sizes': short_positions}
    }

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
        log(f"✅ {side.upper()} {trade_side.upper()} | {quantity} | ID: {order_id}")
        return True
    else:
        log(f"❌ Falhou: {result}")
        return False

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        
        action = data.get('action', '').lower()
        market_position = data.get('marketPosition', '').lower()
        
        log(f"📨 {action.upper()} | {market_position}")
        
        symbol = TARGET_SYMBOL
        price = get_current_price(symbol)
        if not price:
            return jsonify({'status': 'error'}), 500
        
        balance = get_account_balance()
        positions = get_positions(symbol)
        
        log(f"💰 ${balance:.2f} | L:{positions['long']['count']} S:{positions['short']['count']}")
        
        # ======================
        # ABRIR LONG
        # ======================
        if action == 'buy' and market_position == 'long':
            log("🟢 ABRIR LONG")
            
            # Verifica se já tem 2 LONGs (limite pyramiding=2)
            if positions['long']['count'] >= 2:
                log("⚠️ Já tem 2 LONGs (limite)")
                return jsonify({'status': 'ignored'}), 200
            
            # Configura alavancagem
            set_leverage(symbol, LEVERAGE, 'long')
            time.sleep(0.3)
            
            # Calcula 25% do saldo com alavancagem 2x
            if balance <= 0:
                return jsonify({'status': 'error', 'message': 'Sem saldo'}), 500
            
            amount_to_use = balance * POSITION_SIZE_PERCENT  # 25% do saldo
            quantity = round((amount_to_use * LEVERAGE) / price, 4)
            
            log(f"📊 Usar: ${amount_to_use:.2f} (25%) | Comprar: {quantity} WLFI")
            
            success = place_order(symbol, 'buy', 'open', quantity)
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        # ======================
        # ABRIR SHORT
        # ======================
        elif action == 'sell' and market_position == 'short':
            log("🔴 ABRIR SHORT")
            
            # Verifica se já tem 2 SHORTs (limite pyramiding=2)
            if positions['short']['count'] >= 2:
                log("⚠️ Já tem 2 SHORTs (limite)")
                return jsonify({'status': 'ignored'}), 200
            
            # Configura alavancagem
            set_leverage(symbol, LEVERAGE, 'short')
            time.sleep(0.3)
            
            # Calcula 25% do saldo com alavancagem 2x
            if balance <= 0:
                return jsonify({'status': 'error', 'message': 'Sem saldo'}), 500
            
            amount_to_use = balance * POSITION_SIZE_PERCENT  # 25% do saldo
            quantity = round((amount_to_use * LEVERAGE) / price, 4)
            
            log(f"📊 Usar: ${amount_to_use:.2f} (25%) | Vender: {quantity} WLFI")
            
            success = place_order(symbol, 'sell', 'open', quantity)
            return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
        
        # ======================
        # FECHAR LONG (1 posição)
        # ======================
        elif action == 'sell' and market_position == 'flat':
            log("🔵 FECHAR 1 LONG")
            
            if positions['long']['count'] > 0:
                # Fecha a primeira (ou menor) posição LONG
                quantity_to_close = positions['long']['sizes'][0]
                log(f"📊 Fechar: {quantity_to_close} WLFI")
                
                success = place_order(symbol, 'sell', 'close', quantity_to_close)
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            else:
                log("⚠️ Sem LONG para fechar")
                return jsonify({'status': 'warning'}), 200
        
        # ======================
        # FECHAR SHORT (1 posição)
        # ======================
        elif action == 'buy' and market_position == 'flat':
            log("🔵 FECHAR 1 SHORT")
            
            if positions['short']['count'] > 0:
                # Fecha a primeira (ou menor) posição SHORT
                quantity_to_close = positions['short']['sizes'][0]
                log(f"📊 Fechar: {quantity_to_close} WLFI")
                
                success = place_order(symbol, 'buy', 'close', quantity_to_close)
                return jsonify({'status': 'success' if success else 'error'}), 200 if success else 500
            else:
                log("⚠️ Sem SHORT para fechar")
                return jsonify({'status': 'warning'}), 200
        
        else:
            log(f"⏭️ Ignorado: {action}/{market_position}")
            return jsonify({'status': 'ignored'}), 200
    
    except Exception as e:
        log(f"❌ ERRO: {e}")
        import traceback
        log(traceback.format_exc())
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
        <li>Pyramiding: 2 (máx 2 LONG + 2 SHORT)</li>
        <li>Saldo por trade: 25%</li>
        <li>Modo: LONG + SHORT</li>
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
    log("🚀 Bot WLFI - Pyramiding 2 (25% por trade)")
    
    if not all([API_KEY, API_SECRET, API_PASSPHRASE]):
        log("❌ Credenciais faltando!")
        exit(1)
    
    keep_alive()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
