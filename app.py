from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import qrcode
import io
import base64
import os
import json
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
import uuid

load_dotenv()

app = Flask(__name__)
# Fix for Render/Heroku proxy to ensure correct URL generation (https vs http)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

CORS(app)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev_secret_key_change_in_production')

# Spotify Configuration (production-safe)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

# Warn if any required variable is missing
missing = []
if not SPOTIFY_CLIENT_ID:
    missing.append("SPOTIFY_CLIENT_ID")
if not SPOTIFY_CLIENT_SECRET:
    missing.append("SPOTIFY_CLIENT_SECRET")
if not SPOTIFY_REDIRECT_URI:
    missing.append("SPOTIFY_REDIRECT_URI")

if missing:
    print("WARNING: Missing environment variables:", ", ".join(missing))

# Database Handling
DB_FILE = 'music_curator.db'

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS events (
                code TEXT PRIMARY KEY,
                playlist_name TEXT,
                playlist_id TEXT,
                threshold INTEGER,
                admin_id TEXT,
                created_at TEXT,
                active INTEGER,
                admin_token TEXT,
                added_songs TEXT,
                spotify_user_id TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS votes (
                event_code TEXT,
                song_id TEXT,
                user_id TEXT,
                PRIMARY KEY (event_code, song_id, user_id)
            )
        ''')
        conn.commit()

init_db()

# Helper to serialize/deserialize sets and dicts for DB
class DBAdapter:
    @staticmethod
    def adapt_set(s):
        return json.dumps(list(s)) if s else '[]'
    
    @staticmethod
    def convert_set(s):
        return set(json.loads(s)) if s else set()
    
    @staticmethod
    def adapt_json(d):
        return json.dumps(d) if d else '{}'
    
    @staticmethod
    def convert_json(d):
        return json.loads(d) if d else {}

def get_spotify_oauth():
    """Create Spotify OAuth object with correct scope format"""
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope='playlist-modify-public playlist-modify-private'  # Proper space-separated format
    )

def generate_qr_code(event_code):
    """Generate QR code for event"""
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        # Use external URL for QR code
        join_url = url_for('join_event', event_code=event_code, _external=True)
        print(f"[DEBUG] Generating QR for URL: {join_url}")
        qr.add_data(join_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Convert to base64
        img_io = io.BytesIO()
        img.save(img_io, 'PNG')
        img_io.seek(0)
        img_base64 = base64.b64encode(img_io.getvalue()).decode()
        print(f"[DEBUG] QR Code base64 length: {len(img_base64)}")
        return img_base64
    except Exception as e:
        print(f"[ERROR] QR Code generation failed: {e}")
        return None

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin/login')
def admin_login():
    """Admin Spotify login"""
    try:
        sp_oauth = get_spotify_oauth()
        auth_url = sp_oauth.get_authorize_url()
        print(f"[DEBUG] Redirecting to Spotify auth: {auth_url}")
        return redirect(auth_url)
    except Exception as e:
        print(f"[ERROR] Login failed: {e}")
        return render_template('error.html', error=f"Login error: {str(e)}")

@app.route('/callback')
def callback():
    """Spotify OAuth callback"""
    try:
        sp_oauth = get_spotify_oauth()
        code = request.args.get('code')
        error = request.args.get('error')
        
        if error:
            print(f"[ERROR] Spotify error: {error}")
            return render_template('error.html', error=f"Spotify error: {error}")
        
        if not code:
            return render_template('error.html', error="No authorization code received")
        
        print(f"[DEBUG] Received auth code, exchanging for token...")
        token_info = sp_oauth.get_access_token(code)
        
        session['token_info'] = token_info
        user_id = str(uuid.uuid4())
        session['user_id'] = user_id
        # user_tokens removed as we rely on session and event storage
        
        print(f"[DEBUG] Token received, redirecting to dashboard")
        return redirect(url_for('admin_dashboard'))
    except Exception as e:
        print(f"[ERROR] Callback failed: {e}")
        return render_template('error.html', error=f"Authentication error: {str(e)}")

@app.route('/admin/dashboard')
def admin_dashboard():
    """Admin dashboard"""
    if 'token_info' not in session:
        print("[DEBUG] No token in session, redirecting to login")
        return redirect(url_for('admin_login'))
    return render_template('admin_dashboard.html')

@app.route('/api/create-event', methods=['POST'])
def create_event():
    """Create a new voting event"""
    try:
        data = request.json
        event_code = str(uuid.uuid4())[:8].upper()
        
        print(f"[DEBUG] Creating event: {event_code}")
        
        # Get token from session
        token_info = session.get('token_info')
        if not token_info:
            print("[ERROR] No token in session")
            return jsonify({'error': 'Not authenticated. Please login first.'}), 401
        
        # Initialize Spotify client
        print("[DEBUG] Initializing Spotify client...")
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        # Get current user
        print("[DEBUG] Fetching user profile...")
        user_profile = sp.current_user()
        print(f"[DEBUG] User: {user_profile['display_name']}")
        
        # Create or get playlist
        playlist_name = data.get('playlist_name')
        if not playlist_name:
            return jsonify({'error': 'Playlist name is required'}), 400
        
        print(f"[DEBUG] Looking for playlist: {playlist_name}")
        existing_playlists = sp.current_user_playlists()
        
        playlist = None
        for p in existing_playlists['items']:
            if p['name'] == playlist_name:
                playlist = p
                print(f"[DEBUG] Found existing playlist: {p['id']}")
                break
        
        if not playlist:
            print("[DEBUG] Creating new playlist...")
            playlist = sp.user_playlist_create(
                user=user_profile['id'],
                name=playlist_name,
                public=False,
                description='Created by Music Curator'
            )
            print(f"[DEBUG] Created playlist: {playlist['id']}")
        
        # Store event details in DB
        with get_db() as conn:
            conn.execute('''
                INSERT INTO events (
                    code, playlist_name, playlist_id, threshold, admin_id, 
                    created_at, active, admin_token, added_songs, spotify_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                event_code,
                playlist_name,
                playlist['id'],
                int(data.get('threshold', 5)),
                user_profile['id'],
                datetime.now().isoformat(),
                1, # Active
                DBAdapter.adapt_json(token_info),
                DBAdapter.adapt_set(set()),
                user_profile['id']
            ))
            conn.commit()
        
        print(f"[DEBUG] Event created successfully in DB: {event_code}")
        qr_code = generate_qr_code(event_code)
        
        return jsonify({
            'success': True,
            'event_code': event_code,
            'qr_code': qr_code,
            'playlist_name': playlist_name,
            'threshold': int(data.get('threshold', 5))
        })
    
    except Exception as e:
        print(f"[ERROR] Create event failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Error creating event: {str(e)}'}), 400

@app.route('/api/event/<event_code>')
def get_event(event_code):
    """Get event details"""
    with get_db() as conn:
        event = conn.execute('SELECT * FROM events WHERE code = ?', (event_code,)).fetchone()
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        
        # Count all votes for this event
        votes_rows = conn.execute('SELECT song_id, COUNT(*) as count FROM votes WHERE event_code = ? GROUP BY song_id', (event_code,)).fetchall()
        song_votes = {row['song_id']: row['count'] for row in votes_rows}

        # Count total voters
        total_voters = conn.execute('SELECT COUNT(DISTINCT user_id) as count FROM votes WHERE event_code = ?', (event_code,)).fetchone()['count']
        
        return jsonify({
            'code': event['code'],
            'playlist_name': event['playlist_name'],
            'threshold': event['threshold'],
            'votes': song_votes,
            'total_voters': total_voters,
            'user_votes_used': get_user_vote_count(event_code, session.get('voter_id'))
        })

@app.route('/api/event-current-tracks/<event_code>')
def get_event_current_tracks(event_code):
    """Get metadata and votes for tracks already voted in this event"""
    with get_db() as conn:
        event = conn.execute('SELECT * FROM events WHERE code = ?', (event_code,)).fetchone()
    
    if not event:
        return jsonify({'tracks': []})
    
    with get_db() as conn:
        # Get all distinct songs voted for in this event
        voted_songs = conn.execute('SELECT DISTINCT song_id FROM votes WHERE event_code = ?', (event_code,)).fetchall()
        song_ids = [row['song_id'] for row in voted_songs]
    
    if not song_ids:
        return jsonify({'tracks': []})
    
    # Resolve token (prefer admin token from DB)
    token_info = DBAdapter.convert_json(event['admin_token'])
    if not token_info:
        token_info = session.get('token_info')
    if not token_info:
        sp_oauth = get_spotify_oauth()
        token_info = sp_oauth.get_cached_token()
    if not token_info:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        tracks_meta = []
        # Spotify tracks API supports up to 50 IDs per call
        for i in range(0, len(song_ids), 50):
            chunk = song_ids[i:i+50]
            resp = sp.tracks(chunk)
            tracks_meta.extend(resp.get('tracks', []))
        
        voter_id = session.get('voter_id')
        result = []
        
        added_songs = DBAdapter.convert_set(event['added_songs'])
        
        with get_db() as conn:
            # helper to get vote count for each song
            # optimization: we could do one query earlier, but loop is fine for small scale
            # let's just re-fetch all votes for this event to be efficient
            all_votes_rows = conn.execute('SELECT song_id, user_id FROM votes WHERE event_code = ?', (event_code,)).fetchall()
            
            # Organize votes by song
            votes_map = {}
            for row in all_votes_rows:
                sid = row['song_id']
                uid = row['user_id']
                if sid not in votes_map: votes_map[sid] = set()
                votes_map[sid].add(uid)

        for t in tracks_meta:
            if not t: continue 
            sid = t['id']
            curr_votes = votes_map.get(sid, set())
            vote_count = len(curr_votes)
            has_voted = voter_id in curr_votes
            is_added = sid in added_songs
            result.append({
                'id': sid,
                'name': t['name'],
                'artist': ', '.join([a['name'] for a in (t.get('artists') or [])]),
                'image': (t.get('album', {}).get('images', [{}]) or [{}])[0].get('url', ''),
                'spotify_uri': t.get('uri'),
                'votes': vote_count,
                'has_voted': has_voted,
                'is_added': is_added
            })
        
        result.sort(key=lambda x: x['votes'], reverse=True)
        return jsonify({
            'tracks': result,
            'user_votes_used': get_user_vote_count(event_code, voter_id)
        })
    except Exception as e:
        print(f"[ERROR] Fetching tracks failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/join/<event_code>')
def join_event(event_code):
    """Join an event"""
    with get_db() as conn:
        event = conn.execute('SELECT code FROM events WHERE code = ?', (event_code,)).fetchone()
    
    if not event:
        return render_template('error.html', error='Event not found')
    
    session['event_code'] = event_code
    session['voter_id'] = str(uuid.uuid4())
    
    return redirect(url_for('voting_dashboard') + f'?code={event_code}')

@app.route('/voting')
def voting_dashboard():
    """Voting dashboard"""
    # Try to get from query param first, then session
    event_code = request.args.get('code')
    if not event_code:
        event_code = session.get('event_code')
    
    if not event_code:
        # Before redirecting, check one more time if valid
        return redirect(url_for('index'))

    # Verify event exists in DB
    with get_db() as conn:
        event = conn.execute('SELECT code FROM events WHERE code = ?', (event_code,)).fetchone()
    
    if not event:
        print(f"[DEBUG] Invalid or missing event code: {event_code}")
        return redirect(url_for('index'))
    
    # Ensure session is synced
    if session.get('event_code') != event_code:
        print(f"[DEBUG] Syncing session for event: {event_code}")
        session['event_code'] = event_code
    
    # Ensure voter_id exists
    if 'voter_id' not in session:
        session['voter_id'] = str(uuid.uuid4())
    
    return render_template('voting_dashboard.html')

def ensure_valid_token(token_info):
    """Check if token is expired and refresh if necessary"""
    try:
        sp_oauth = get_spotify_oauth()
        if sp_oauth.is_token_expired(token_info):
            print(f"[DEBUG] Token expired, refreshing...")
            new_token = sp_oauth.refresh_access_token(token_info['refresh_token'])
            return new_token
    except Exception as e:
        print(f"[ERROR] Token refresh failed: {e}")
    return token_info

@app.route('/api/search-songs', methods=['POST'])
def search_songs():
    """Search Spotify songs"""
    try:
        data = request.json
        query = data.get('query')
        
        if not query:
            print("[WARN] Search query missing")
            return jsonify({'error': 'Query is required'}), 400
        
        print(f"[DEBUG] Processing search for: '{query}'")
        
        event_code = session.get('event_code')
        token_info = None
        source = "None"
        
        # 1. Try Session Token
        if 'token_info' in session:
            token_info = session['token_info']
            source = "Session"
        
        # 2. Try Admin Token Fallback (if event exists)
        event = None
        if not token_info and event_code:
            with get_db() as conn:
                event = conn.execute('SELECT * FROM events WHERE code = ?', (event_code,)).fetchone()
            
            if event:
                token_info = DBAdapter.convert_json(event['admin_token'])
                source = "Event Admin"
            
        if not token_info:
            print("[ERROR] No authentication token found in session or event")
            return jsonify({'error': 'Not authenticated. Please join an active event.'}), 401
            
        # 3. Ensure Token Validity
        token_info = ensure_valid_token(token_info)
        
        # Update session/event if refreshed
        if source == "Session":
            session['token_info'] = token_info
        elif source == "Event Admin" and event:
            # Update DB with new token
            with get_db() as conn:
                conn.execute('UPDATE events SET admin_token = ? WHERE code = ?', 
                             (DBAdapter.adapt_json(token_info), event_code))
                conn.commit()

        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        print(f"[DEBUG] Calling Spotify Search API (Source: {source})...")
        results = sp.search(q=query, type='track', limit=10)
        
        tracks = []
        voter_id = session.get('voter_id')
        
        # Get vote counts for this event
        event_votes_map = {}
        added_songs = set()
        
        if event_code:
            with get_db() as conn:
                votes_rows = conn.execute('SELECT song_id, user_id FROM votes WHERE event_code = ?', (event_code,)).fetchall()
                for row in votes_rows:
                    sid = row['song_id']
                    if sid not in event_votes_map: event_votes_map[sid] = set()
                    event_votes_map[sid].add(row['user_id'])
                
                # refresh event to get added songs
                if not event:
                    event = conn.execute('SELECT added_songs FROM events WHERE code = ?', (event_code,)).fetchone()
                if event:
                    added_songs = DBAdapter.convert_set(event['added_songs'])

        items = results.get('tracks', {}).get('items', [])
        print(f"[DEBUG] Spotify returned {len(items)} items")

        for track in items:
            tid = track['id']
            curr_votes = event_votes_map.get(tid, set())
            vote_count = len(curr_votes)
            has_voted = voter_id in curr_votes if voter_id else False
            is_added = tid in added_songs
            
            tracks.append({
                'id': tid,
                'name': track['name'],
                'artist': ', '.join([artist['name'] for artist in track['artists']]),
                'image': track['album']['images'][0]['url'] if track['album']['images'] else '',
                'spotify_uri': track['uri'],
                'votes': vote_count,
                'has_voted': has_voted,
                'is_added': is_added
            })
        
        return jsonify({
            'tracks': tracks,
            'user_votes_used': get_user_vote_count(event_code, voter_id)
        })
    
    except spotipy.exceptions.SpotifyException as se:
        print(f"[ERROR] Spotify API Error: {se}")
        return jsonify({'error': f"Spotify Error: {se.msg}"}), 400
    except Exception as e:
        print(f"[ERROR] Search failed with exception: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Search error: {str(e)}'}), 400

def get_user_vote_count(event_code, voter_id):
    """Count how many active votes a user has in an event"""
    if not event_code or not voter_id:
        return 0
    
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) as count FROM votes WHERE event_code = ? AND user_id = ?', (event_code, voter_id)).fetchone()['count']
    return count

@app.route('/api/vote', methods=['POST'])
def vote():
    """Vote for a song"""
    try:
        data = request.json
        event_code = data.get('event_code')
        song_id = data.get('song_id')
        voter_id = session.get('voter_id')
        
        if not event_code or not song_id or not voter_id:
            return jsonify({'error': 'Missing event, song, or voter ID'}), 400
        
        with get_db() as conn:
            event = conn.execute('SELECT * FROM events WHERE code = ?', (event_code,)).fetchone()
            
            if not event:
                return jsonify({'error': 'Invalid event'}), 400
        
            # Check if song is already added
            added_songs = DBAdapter.convert_set(event['added_songs'])
            if song_id in added_songs:
                return jsonify({
                    'success': False,
                    'error': 'Song already added to playlist',
                    'is_added': True
                })

            print(f"[DEBUG] Vote from {voter_id} for song {song_id} in event {event_code}")
            
            # Check if user already voted for this song
            existing_vote = conn.execute('SELECT 1 FROM votes WHERE event_code = ? AND song_id = ? AND user_id = ?', 
                                         (event_code, song_id, voter_id)).fetchone()
            is_removing = existing_vote is not None
            
            # Check vote limit (Max 3) - Only if adding
            if not is_removing:
                user_vote_count = conn.execute('SELECT COUNT(*) as count FROM votes WHERE event_code = ? AND user_id = ?', 
                                              (event_code, voter_id)).fetchone()['count']
                if user_vote_count >= 3:
                    return jsonify({
                        'success': False, 
                        'error': 'You have used all 3 votes!',
                        'vote_limit_reached': True
                    })

            # Toggle vote
            if is_removing:
                conn.execute('DELETE FROM votes WHERE event_code = ? AND song_id = ? AND user_id = ?',
                             (event_code, song_id, voter_id))
                action = 'removed'
            else:
                conn.execute('INSERT INTO votes (event_code, song_id, user_id) VALUES (?, ?, ?)',
                             (event_code, song_id, voter_id))
                action = 'added'
            conn.commit()
            
            # Get updated stats
            vote_count = conn.execute('SELECT COUNT(*) as count FROM votes WHERE event_code = ? AND song_id = ?',
                                      (event_code, song_id)).fetchone()['count']
            user_votes_used = conn.execute('SELECT COUNT(*) as count FROM votes WHERE event_code = ? AND user_id = ?',
                                          (event_code, voter_id)).fetchone()['count']
            
            threshold = event['threshold']
            print(f"[DEBUG] Vote count: {vote_count}, Threshold: {threshold}")
            
            # Check threshold
            if vote_count >= threshold:
                try:
                    # Resolve admin token
                    token_info = DBAdapter.convert_json(event['admin_token'])
                    # We might need to refresh it here too, similar to search
                    token_info = ensure_valid_token(token_info)
                    
                    if token_info:
                        print(f"[DEBUG] Threshold reached! Adding song to playlist...")
                        sp = spotipy.Spotify(auth=token_info['access_token'])
                        playlist_id = event['playlist_id']
                        sp.playlist_add_items(playlist_id, [f'spotify:track:{song_id}'])
                        
                        # Mark as added
                        added_songs.add(song_id)
                        conn.execute('UPDATE events SET added_songs = ? WHERE code = ?',
                                     (DBAdapter.adapt_set(added_songs), event_code))
                        conn.commit()
                        
                        print(f"[DEBUG] Song added to playlist successfully")
                        
                        return jsonify({
                            'success': True,
                            'action': action,
                            'vote_count': vote_count,
                            'user_votes_used': user_votes_used,
                            'threshold_reached': True,
                            'message': 'Song added to playlist!'
                        })
                except Exception as e:
                    print(f"[ERROR] Failed to add song to playlist: {e}")
                    return jsonify({'error': f'Could not add to playlist: {str(e)}'}), 400
        
        return jsonify({
            'success': True,
            'action': action,
            'vote_count': vote_count,
            'user_votes_used': user_votes_used,
            'threshold_reached': False
        })
    
    except Exception as e:
        print(f"[ERROR] Vote failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 400

@app.route('/api/event-stats/<event_code>')
def get_event_stats(event_code):
    """Get real-time event statistics"""
    with get_db() as conn:
        event = conn.execute('SELECT threshold FROM events WHERE code = ?', (event_code,)).fetchone()
        
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        
        # Get counts for all songs
        rows = conn.execute('SELECT song_id, COUNT(*) as count FROM votes WHERE event_code = ? GROUP BY song_id', (event_code,)).fetchall()
        
        total_voters = conn.execute('SELECT COUNT(DISTINCT user_id) as count FROM votes WHERE event_code = ?', (event_code,)).fetchone()['count']

    songs_data = []
    for row in rows:
        songs_data.append({
            'song_id': row['song_id'],
            'votes': row['count'],
            'threshold': event['threshold']
        })
    
    songs_data.sort(key=lambda x: x['votes'], reverse=True)
    
    return jsonify({
        'songs': songs_data,
        'total_voters': total_voters,
        'threshold': event['threshold']
    })

if __name__ == '__main__':
    # Use 0.0.0.0 to be accessible from other devices in the network
    print("[DEBUG] Starting Flask app on http://0.0.0.0:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)