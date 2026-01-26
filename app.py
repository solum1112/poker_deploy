import eventlet
eventlet.monkey_patch()  # 반드시 최상단에 위치해야 합니다

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import random   
import itertools
import uuid
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'poker_secret_1234'

# 소켓 설정 최적화
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='eventlet',
    ping_timeout=60,
    ping_interval=25
)

class Card:
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank
    def __repr__(self):
        ranks = {11: 'J', 12: 'Q', 13: 'K', 14: 'A'}
        r = ranks.get(self.rank, str(self.rank))
        return f"{self.suit}{r}"

class Deck:
    def __init__(self):
        self.deck = [Card(s, r) for s in ["♠", "♥", "♦", "♣"] for r in range(2, 15)] 
        random.shuffle(self.deck)
    def deal(self):
        return self.deck.pop()

class Player:
    def __init__(self, name, chips, sid, p_uuid):
        self.name = name
        self.chips = chips
        self.sid = sid
        self.uuid = p_uuid
        self.hand = []
        self.bet_this_round = 0
        self.total_bet = 0 
        self.is_folded = False
        self.is_all_in = False
        self.has_acted = False
        self.chips_at_start = chips 
        self.current_hand_name = ""
        self.pos_name = ""
        self.status = 'waiting'

    def bet(self, amount):
        if amount >= self.chips:
            amount = self.chips
            self.is_all_in = True 
        self.chips -= amount
        self.bet_this_round += amount
        self.total_bet += amount 
        return amount

    def to_dict(self):
        round_profit = self.chips - self.chips_at_start
        return {
            'uuid': self.uuid,
            'name': self.name,
            'chips': self.chips,
            'round_profit': round_profit,
            'hand': [str(c) for c in self.hand],
            'bet_this_round': self.bet_this_round,
            'is_folded': self.is_folded,
            'is_all_in': self.is_all_in,
            'has_acted': self.has_acted,
            'pos_name': self.pos_name,
            'status': self.status,
            'current_hand_name': self.current_hand_name
        }

class PotManager:
    def calculate_side_pots(self, players):
        pots = [] 
        active_bettors = sorted([p for p in players if p.total_bet > 0], key=lambda p: p.total_bet)
        last_level = 0
        for i, p in enumerate(active_bettors):
            if p.total_bet > last_level:
                amount = sum(min(p2.total_bet - last_level, p.total_bet - last_level) 
                             for p2 in active_bettors if p2.total_bet > last_level)
                eligible = [p2.name for p2 in active_bettors[i:] if not p2.is_folded]
                if eligible: pots.append({'amount': amount, 'eligible': eligible})
                last_level = p.total_bet
        return pots

class HandEvaluator:
    HAND_NAMES = {8: "스트레이트 플러쉬", 7: "포카드", 6: "풀하우스", 5: "플러쉬", 4: "스트레이트", 3: "트리플", 2: "투페어", 1: "원페어", 0: "하이카드"}
    def evaluate_5_cards(self, cards):
        ranks = sorted([c.rank for c in cards], reverse=True)
        suits = [c.suit for c in cards]
        rank_counts = {r: ranks.count(r) for r in set(ranks)}
        counts = sorted(rank_counts.values(), reverse=True)
        is_flush = len(set(suits)) == 1
        is_straight = len(set(ranks)) == 5 and (max(ranks) - min(ranks) == 4)
        if not is_straight and set(ranks) == {14, 5, 4, 3, 2}:
            is_straight = True
            ranks = [5, 4, 3, 2, 1]
        if is_flush and is_straight: return (8, ranks)
        if counts == [4, 1]: return (7, sorted(rank_counts, key=lambda x: (rank_counts[x], x), reverse=True))
        if counts == [3, 2]: return (6, sorted(rank_counts, key=lambda x: (rank_counts[x], x), reverse=True))
        if is_flush: return (5, ranks)
        if is_straight: return (4, ranks)
        if counts == [3, 1, 1]: return (3, sorted(rank_counts, key=lambda x: (rank_counts[x], x), reverse=True))
        if counts == [2, 2, 1]: return (2, sorted(rank_counts, key=lambda x: (rank_counts[x], x), reverse=True))
        if counts == [2, 1, 1, 1]: return (1, sorted(rank_counts, key=lambda x: (rank_counts[x], x), reverse=True))
        return (0, ranks)
    def get_best_hand(self, hole_cards, community_cards):
        all_cards = hole_cards + community_cards
        best_score = (-1, [])
        for combo in itertools.combinations(all_cards, 5):
            score = self.evaluate_5_cards(combo)
            if score > best_score: best_score = score
        return best_score

# 전역 상태
player_list = []
community_cards = []
current_deck = None
winner_result = None
turn_idx = 0
high_bet = 0
pot = 0
dealer_idx = -1

def broadcast_game_state():
    state = {
        'players': [p.to_dict() for p in player_list],
        'community': [str(c) for c in community_cards],
        'pot': pot,
        'high_bet': high_bet,
        'turn_idx': turn_idx % len(player_list) if player_list else 0,
        'winner_result': winner_result
    }
    socketio.emit('update_game', state)

def change_rank_to_str(rank):
    rank_dict = {11: 'J', 12: 'Q', 13: 'K', 14: 'A'}
    return str(rank_dict.get(rank, rank))

@app.route('/')
def index():
    players_data = [p.to_dict() for p in player_list]
    return render_template('index.html', 
                           players=players_data, 
                           community=[str(c) for c in community_cards], 
                           pot=pot,
                           winner_result=winner_result, 
                           turn_idx=turn_idx, 
                           high_bet=high_bet)

@socketio.on('join_game')
def handle_join(data):
    global player_list
    name = data.get('player_name', '').strip()
    client_uuid = data.get('p_uuid')
    current_sid = request.sid
    if client_uuid and any(p.uuid == client_uuid for p in player_list): return
    if name and len(player_list) < 6:
        new_uuid = str(uuid.uuid4())
        new_player = Player(name, 5000, current_sid, new_uuid)
        player_list.append(new_player)
        emit('join_success', {'p_uuid': new_uuid}, room=current_sid)
        broadcast_game_state()

@socketio.on('start_game')
def handle_start():
    global current_deck, community_cards, winner_result, turn_idx, high_bet, player_list, dealer_idx, pot
    active_in_game = [p for p in player_list if p.chips > 0]
    if len(active_in_game) < 2:
        winner_result = "인원이 부족합니다 (칩 리셋 필요)"
        broadcast_game_state()
        return
    winner_result = None
    community_cards = []
    current_deck = Deck()
    pot = 0
    high_bet = 200
    num_p = len(player_list)
    dealer_idx = (dealer_idx + 1) % num_p
    sb_idx = (dealer_idx + 1) % num_p
    bb_idx = (dealer_idx + 2) % num_p
    if num_p == 2:
        sb_idx = dealer_idx
        bb_idx = (dealer_idx + 1) % 2
        turn_idx = sb_idx
    else:
        turn_idx = (dealer_idx + 3) % num_p
    for i, p in enumerate(player_list):
        p.hand = [current_deck.deal(), current_deck.deal()]
        p.bet_this_round = p.total_bet = 0
        p.is_all_in = p.is_folded = p.has_acted = False
        p.chips_at_start = p.chips
        p.status = 'waiting'
        p.pos_name = 'D' if i == dealer_idx else ('SB' if i == sb_idx else ('BB' if i == bb_idx else ''))
        if i == sb_idx: pot += p.bet(100)
        elif i == bb_idx: pot += p.bet(200)
    broadcast_game_state()

@socketio.on('reset_game') # 리셋 기능 추가
def handle_reset():
    global player_list, community_cards, winner_result, pot, high_bet
    for p in player_list:
        p.chips = 5000
        p.hand = []
        p.is_folded = p.is_all_in = False
        p.status = 'waiting'
    community_cards = []
    winner_result = "게임 초기화됨"
    pot = high_bet = 0
    broadcast_game_state()

@socketio.on('player_action')
def handle_action(data):
    global turn_idx, high_bet, pot, community_cards, winner_result
    client_uuid = data.get('p_uuid')
    current_player = player_list[turn_idx % len(player_list)]
    if current_player.uuid != client_uuid: return
    action_type = data.get('type')
    p = player_list[turn_idx % len(player_list)]
    p.has_acted = True
    if action_type == 'fold':
        p.is_folded = True
        p.status = 'Fold'
    elif action_type == 'allin':
        pot += p.bet(p.chips)
        p.status = 'All-In'
        if p.bet_this_round > high_bet:
            high_bet = p.bet_this_round
            for op in player_list:
                if op != p: op.has_acted = False
    elif action_type == 'raise':
        try:
            target = int(data.get('amount'))
            pot += p.bet(target - p.bet_this_round)
            high_bet = p.bet_this_round
            for op in player_list:
                if op != p: op.has_acted = False
            p.status = 'Raise'
        except: return
    else:
        diff = high_bet - p.bet_this_round
        p.status = 'Check' if diff == 0 else 'Call'
        pot += p.bet(diff)
    active_players = [p for p in player_list if not p.is_folded]
    if len(active_players) == 1:
        winner_result = f'{active_players[0].name} 승리! (기권)'
        active_players[0].chips += pot
        broadcast_game_state()
        return
    while True:
        turn_idx += 1
        next_p = player_list[turn_idx % len(player_list)]
        round_over = all(p.is_all_in or (p.bet_this_round == high_bet and p.has_acted) for p in active_players)    
        if round_over or (not next_p.is_folded and not next_p.is_all_in): break
    if round_over: process_round_end(active_players)
    else: broadcast_game_state()

def process_round_end(active_players):
    global community_cards, high_bet, turn_idx, pot
    not_all_in = [p for p in active_players if not p.is_all_in]
    if len(not_all_in) <= 1:
        while len(community_cards) < 5: community_cards.append(current_deck.deal())
        run_showdown()
        return
    for p in player_list:
        p.bet_this_round = 0
        p.has_acted = False
        p.status = 'waiting'
    high_bet = 0
    turn_idx = (dealer_idx + 1) % len(player_list)
    while player_list[turn_idx % len(player_list)].is_folded: turn_idx += 1
    if len(community_cards) == 0:
        for _ in range(3): community_cards.append(current_deck.deal())
    elif len(community_cards) < 5:
        community_cards.append(current_deck.deal())
    else:
        run_showdown()
        return
    broadcast_game_state()

def run_showdown():
    global winner_result, pot
    pm = PotManager()
    eval = HandEvaluator()
    side_pots = pm.calculate_side_pots(player_list)
    for pot_info in side_pots:
        p_best = (-1, [])
        p_winners = []
        for name in pot_info['eligible']:
            p = next(p for p in player_list if p.name == name)
            s = eval.get_best_hand(p.hand, community_cards)
            if s > p_best: p_best = s ; p_winners = [p]
            elif s == p_best: p_winners.append(p)
        share = pot_info['amount'] // len(p_winners)
        for w in p_winners: w.chips += share
    active = [p for p in player_list if not p.is_folded]
    top_score = (-1, [])
    top_winners = []
    for p in active:
        score = eval.get_best_hand(p.hand, community_cards)
        p.current_hand_name = f"{change_rank_to_str(score[1][0])} {eval.HAND_NAMES[score[0]]}"
        if score > top_score: top_score = score; top_winners = [p.name]
        elif score == top_score: top_winners.append(p.name)   
    winner_result = f'{", ".join(top_winners)} 승리! ({change_rank_to_str(top_score[1][0])} {eval.HAND_NAMES[top_score[0]]})'
    broadcast_game_state()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
