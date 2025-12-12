from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_cors import CORS
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import qrcode
import io
import base64
import os
import json
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

# In-memory storage (replace with database in production)
events = {}
votes = {}
user_tokens = {}

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
        user_tokens[user_id] = token_info
        
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
        
        # Store event details
        events[event_code] = {
            'code': event_code,
            'playlist_name': playlist_name,
            'playlist_id': playlist['id'],
            'threshold': int(data.get('threshold', 5)),
            'admin_id': user_profile['id'],
            'created_at': datetime.now().isoformat(),
            'active': True,
            'spotify_user_id': user_profile['id'],
            'admin_token': token_info,
            'added_songs': set()
        }
        
        votes[event_code] = {}
        
        print(f"[DEBUG] Event created successfully: {event_code}")
        qr_code = generate_qr_code(event_code)
        
        return jsonify({
            'success': True,
            'event_code': event_code,
            'qr_code': qr_code,
            'playlist_name': playlist_name,
            'threshold': events[event_code]['threshold']
        })
    
    except Exception as e:
        print(f"[ERROR] Create event failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Error creating event: {str(e)}'}), 400

@app.route('/api/event/<event_code>')
def get_event(event_code):
    """Get event details"""
    if event_code not in events:
        return jsonify({'error': 'Event not found'}), 404
    
    event = events[event_code]
    event_votes = votes.get(event_code, {})
    
    # Count votes per song
    song_votes = {}
    for song_id, voters in event_votes.items():
        song_votes[song_id] = len(voters)
    
    return jsonify({
        'code': event['code'],
        'playlist_name': event['playlist_name'],
        'threshold': event['threshold'],
        'votes': song_votes,
        'total_voters': len(set(voter for voters in event_votes.values() for voter in voters)),
        'user_votes_used': get_user_vote_count(event_code, session.get('voter_id'))
    })

@app.route('/api/event-current-tracks/<event_code>')
def get_event_current_tracks(event_code):
    """Get metadata and votes for tracks already voted in this event"""
    if event_code not in events:
        return jsonify({'tracks': []})
    
    event_votes = votes.get(event_code, {})
    song_ids = list(event_votes.keys())
    if not song_ids:
        return jsonify({'tracks': []})
    
    # Resolve token (prefer admin token)
    token_info = events.get(event_code, {}).get('admin_token')
    if not token_info:
        token_info = session.get('token_info')
    if not token_info:
        sp_oauth = get_spotify_oauth()
        token_info = sp_oauth.get_cached_token()
    if not token_info:
        return jsonify({'error': 'Not authenticated'}), 401
    
    sp = spotipy.Spotify(auth=token_info['access_token'])
    
    tracks_meta = []
    # Spotify tracks API supports up to 50 IDs per call
    for i in range(0, len(song_ids), 50):
        chunk = song_ids[i:i+50]
        resp = sp.tracks(chunk)
        tracks_meta.extend(resp.get('tracks', []))
    
    voter_id = session.get('voter_id')
    result = []
    for t in tracks_meta:
        sid = t['id']
        vote_count = len(event_votes.get(sid, []))
        has_voted = voter_id in event_votes.get(sid, set())
        is_added = sid in events[event_code].get('added_songs', set())
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
    result.sort(key=lambda x: x['votes'], reverse=True)
    return jsonify({
        'tracks': result,
        'user_votes_used': get_user_vote_count(event_code, voter_id)
    })

@app.route('/join/<event_code>')
def join_event(event_code):
    """Join an event"""
    if event_code not in events:
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
    
    if not event_code or event_code not in events:
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
        if not token_info and event_code and event_code in events:
            token_info = events[event_code].get('admin_token')
            source = "Event Admin"
            
        if not token_info:
            print("[ERROR] No authentication token found in session or event")
            return jsonify({'error': 'Not authenticated. Please join an active event.'}), 401
            
        # 3. Ensure Token Validity
        token_info = ensure_valid_token(token_info)
        
        # Update session/event if refreshed
        if source == "Session":
            session['token_info'] = token_info
        elif source == "Event Admin":
            events[event_code]['admin_token'] = token_info

        sp = spotipy.Spotify(auth=token_info['access_token'])
        
        print(f"[DEBUG] Calling Spotify Search API (Source: {source})...")
        results = sp.search(q=query, type='track', limit=10)
        
        tracks = []
        voter_id = session.get('voter_id')
        event_votes = votes.get(event_code, {}) if event_code else {}
        
        items = results.get('tracks', {}).get('items', [])
        print(f"[DEBUG] Spotify returned {len(items)} items")

        for track in items:
            vote_count = len(event_votes.get(track['id'], []))
            has_voted = voter_id in event_votes.get(track['id'], set())
            is_added = track['id'] in events[event_code].get('added_songs', set()) if event_code in events else False
            tracks.append({
                'id': track['id'],
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
    
    except Exception as e:
        print(f"[ERROR] Search failed with exception: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Search error: {str(e)}'}), 400

def get_user_vote_count(event_code, voter_id):
    """Count how many active votes a user has in an event"""
    if event_code not in votes:
        return 0
    
    count = 0
    for song_votes in votes[event_code].values():
        if voter_id in song_votes:
            count += 1
    return count

@app.route('/api/vote', methods=['POST'])
def vote():
    """Vote for a song"""
    try:
        data = request.json
        event_code = data.get('event_code')
        song_id = data.get('song_id')
        voter_id = session.get('voter_id')
        
        if not event_code or event_code not in events:
            return jsonify({'error': 'Invalid event'}), 400
        
        if not song_id or not voter_id:
            return jsonify({'error': 'Missing song or voter ID'}), 400
        
        # Check if song is already added
        if song_id in events[event_code].get('added_songs', set()):
            print(f"[DEBUG] Song {song_id} already added, rejecting vote")
            return jsonify({
                'success': False,
                'error': 'Song already added to playlist',
                'is_added': True
            })

        print(f"[DEBUG] Vote from {voter_id} for song {song_id} in event {event_code}")
        
        if event_code not in votes:
            votes[event_code] = {}
        
        if song_id not in votes[event_code]:
            votes[event_code][song_id] = set()
        
        # Check vote limit (Max 3)
        user_votes = get_user_vote_count(event_code, voter_id)
        is_removing = voter_id in votes[event_code][song_id]
        
        if not is_removing and user_votes >= 3:
            return jsonify({
                'success': False, 
                'error': 'You have used all 3 votes!',
                'vote_limit_reached': True
            })

        # Toggle vote
        if is_removing:
            votes[event_code][song_id].remove(voter_id)
            action = 'removed'
            user_votes -= 1 # Updated count
        else:
            votes[event_code][song_id].add(voter_id)
            action = 'added'
            user_votes += 1 # Updated count
        
        vote_count = len(votes[event_code][song_id])
        threshold = events[event_code]['threshold']
        
        print(f"[DEBUG] Vote count: {vote_count}, Threshold: {threshold}")
        
        # Check if threshold reached and song not already added
        if vote_count >= threshold and song_id not in events[event_code].get('added_songs', set()):
            try:
                token_info = events.get(event_code, {}).get('admin_token')
                if not token_info:
                    token_info = session.get('token_info')
                if not token_info:
                    sp_oauth = get_spotify_oauth()
                    token_info = sp_oauth.get_cached_token()
                
                if token_info:
                    print(f"[DEBUG] Threshold reached! Adding song to playlist...")
                    sp = spotipy.Spotify(auth=token_info['access_token'])
                    playlist_id = events[event_code]['playlist_id']
                    sp.playlist_add_items(playlist_id, [f'spotify:track:{song_id}'])
                    
                    # Mark as added
                    if 'added_songs' not in events[event_code]:
                        events[event_code]['added_songs'] = set()
                    events[event_code]['added_songs'].add(song_id)
                    
                    print(f"[DEBUG] Song added to playlist successfully")
                    
                    return jsonify({
                        'success': True,
                        'action': action,
                        'vote_count': vote_count,
                        'user_votes_used': user_votes,
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
            'user_votes_used': user_votes,
            'threshold_reached': False
        })
    
    except Exception as e:
        print(f"[ERROR] Vote failed: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/event-stats/<event_code>')
def get_event_stats(event_code):
    """Get real-time event statistics"""
    if event_code not in events:
        return jsonify({'error': 'Event not found'}), 404
    
    event_votes = votes.get(event_code, {})
    
    # Get all songs with vote counts
    songs_data = []
    for song_id, voters in event_votes.items():
        songs_data.append({
            'song_id': song_id,
            'votes': len(voters),
            'threshold': events[event_code]['threshold']
        })
    
    songs_data.sort(key=lambda x: x['votes'], reverse=True)
    
    return jsonify({
        'songs': songs_data,
        'total_voters': len(set(voter for voters in event_votes.values() for voter in voters)),
        'threshold': events[event_code]['threshold']
    })

if __name__ == '__main__':
    print("[DEBUG] Starting Flask app on http://127.0.0.1:5000")
    app.run(debug=True, host='127.0.0.1', port=5000)