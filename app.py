from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import math
import json
import sqlite3
import hashlib
import re
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure post is not empty
    if content and content.strip():
        # Coding Assignment 3, Design Claim 25: progressive access for newcomers
        access_problem = check_newcomer_access('post')
        if access_problem:
            flash(access_problem, 'warning')
            return redirect(url_for('newcomer_path'))

        # Coding Assignment 2, Design Claim 20: if the post breaks a rule, show the
        # reminder page first instead of publishing it.
        reminder_page = check_before_publishing('post', content)
        if reminder_page:
            return reminder_page

        # The original text is stored; moderate_content() is applied every time the
        # post is displayed. This keeps the admin risk scores correct (they are
        # calculated from the original text) and lets rule changes apply to old posts.
        db = get_db()
        db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
                   (user_id, content))
        db.commit()
        resolve_reminder(content)  # log the outcome if the user came via the reminder
        flash('Your post was successfully created!', 'success')
    else:
        # This will catch empty posts or posts that were fully censored
        flash('Post cannot be empty or was fully censored.', 'warning')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    
    
@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and send the newcomer to the Newcomer Path
            #    (Coding Assignment 3, Design Claim 22: formal onboarding from the start)
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('newcomer_path'))

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content')

    # Basic validation to ensure comment is not empty
    if content and content.strip():
        # Coding Assignment 2, Design Claim 20: reminder before a rule-breaking comment
        reminder_page = check_before_publishing('comment', content, post_id=post_id)
        if reminder_page:
            return reminder_page

        db = get_db()
        db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
                   (post_id, user_id, content))
        db.commit()
        came_from_reminder = request.form.get('prompt_id') is not None
        resolve_reminder(content)
        flash('Your comment was added.', 'success')
        # After the reminder page the referrer is the reminder itself, so go to the post
        if came_from_reminder:
            return redirect(url_for('post_detail', post_id=post_id))
    else:
        flash('Comment cannot be empty.', 'warning')

    # Redirect back to the page the user came from (likely the post detail page)
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           PAGE_SIZE=PAGE_SIZE,

                           # Coding Assignment 2, Design Claim 20: reminder statistics
                           reminder_stats=get_reminder_stats(),
                           recent_reminders=get_recent_reminders())



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Functions to be implemented are below

# Coding Assignment #1
# Chapter 3 Design Claim 14: friends of friends


@app.route('/people-you-may-know')
def people_you_may_know():
    """Shows 'friends of friends' suggestions for the logged-in user."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to see suggestions.', 'danger')
        return redirect(url_for('login'))

    # Find users who are followed by the people I follow (friends of friends).
    # - f1: the people I follow
    # - f2: the people THEY follow (= candidates)
    # We leave out myself and users I already follow, and count how many
    # of my followed users follow each candidate (= mutual connections).
    candidates = query_db('''
        SELECT u.id, u.username, u.profile,
               COUNT(DISTINCT f1.followed_id) AS mutual_count
        FROM follows f1
        JOIN follows f2 ON f2.follower_id = f1.followed_id
        JOIN users u ON u.id = f2.followed_id
        WHERE f1.follower_id = ?
          AND f2.followed_id != ?
          AND f2.followed_id NOT IN (
              SELECT followed_id FROM follows WHERE follower_id = ?
          )
        GROUP BY u.id
        ORDER BY mutual_count DESC, u.username ASC
        LIMIT 10
    ''', (user_id, user_id, user_id))

    suggestions = []
    for candidate in candidates:
        suggestion = dict(candidate)

        # Names of (up to 3) people I follow who also follow this candidate.
        # Showing familiar names explains WHY the person is suggested.
        mutuals = query_db('''
            SELECT u.username
            FROM follows f1
            JOIN follows f2 ON f2.follower_id = f1.followed_id
            JOIN users u ON u.id = f1.followed_id
            WHERE f1.follower_id = ? AND f2.followed_id = ?
            LIMIT 3
        ''', (user_id, suggestion['id']))
        suggestion['mutual_names'] = [m['username'] for m in mutuals]

        # The candidate's latest post gives a conversation starter,
        # so the user can interact with them right away.
        latest_post = query_db('''
            SELECT id, content, created_at FROM posts
            WHERE user_id = ? ORDER BY created_at DESC LIMIT 1
        ''', (suggestion['id'],), one=True)
        if latest_post:
            post = dict(latest_post)
            post['content'], _ = moderate_content(post['content'])
            suggestion['latest_post'] = post
        else:
            suggestion['latest_post'] = None

        # Moderate the profile text the same way the profile page does
        suggestion['profile'], _ = moderate_content(suggestion.get('profile') or '')
        suggestions.append(suggestion)

    # How many users I follow, to show a helpful message if it is zero
    following_count = query_db('SELECT COUNT(*) AS cnt FROM follows WHERE follower_id = ?',
                               (user_id,), one=True)['cnt']

    return render_template('people_you_may_know.html.j2',
                           suggestions=suggestions,
                           following_count=following_count)



# Chapter 3, Design Claim 31: "Highlighting opportunities to return favors to
# specific others increases normative commitment to the community."
# This page lists the people who have reacted to or commented on YOUR posts,
# and shows their posts that you have not yet engaged with, so you can
# easily return the favor (react or comment) right on the page.

@app.route('/pay-it-back')
def pay_it_back():
    """Shows posts from users who have engaged with the current user's posts."""
    user_id = session.get('user_id')

    if not user_id:
        flash('You must be logged in to see this page.', 'danger')
        return redirect(url_for('login'))

    # Step 1: Find "helpers" = users who have reacted to or commented on
    # my posts. We count reactions and comments separately so we can show
    # exactly what each person has done for me.
    helpers = query_db('''
        SELECT u.id, u.username,
               SUM(engagement.is_reaction) AS reaction_count,
               SUM(1 - engagement.is_reaction) AS comment_count,
               COUNT(*) AS total_count
        FROM (
            -- Reactions others have given to my posts
            SELECT r.user_id, 1 AS is_reaction
            FROM reactions r JOIN posts p ON r.post_id = p.id
            WHERE p.user_id = ? AND r.user_id != ?
            UNION ALL
            -- Comments others have written on my posts
            SELECT c.user_id, 0 AS is_reaction
            FROM comments c JOIN posts p ON c.post_id = p.id
            WHERE p.user_id = ? AND c.user_id != ?
        ) AS engagement
        JOIN users u ON u.id = engagement.user_id
        GROUP BY u.id
        ORDER BY total_count DESC, u.username ASC
    ''', (user_id, user_id, user_id, user_id))

    favors = []
    for helper in helpers:
        helper_dict = dict(helper)

        # Step 2: For each helper, find their latest posts that I have NOT
        # yet reacted to or commented on. These are the "favors to return".
        posts_raw = query_db('''
            SELECT p.id, p.content, p.created_at
            FROM posts p
            WHERE p.user_id = ?
              AND p.id NOT IN (SELECT post_id FROM reactions WHERE user_id = ?)
              AND p.id NOT IN (SELECT post_id FROM comments WHERE user_id = ?)
            ORDER BY p.created_at DESC
            LIMIT 3
        ''', (helper_dict['id'], user_id, user_id))

        # Skip helpers whose posts I have already engaged with (nothing to return)
        if not posts_raw:
            continue

        posts = []
        for post_raw in posts_raw:
            post = dict(post_raw)
            post['content'], _ = moderate_content(post['content'])
            posts.append(post)
        helper_dict['posts'] = posts
        favors.append(helper_dict)

        # Keep the page short: show the 10 most helpful people
        if len(favors) >= 10:
            break

    return render_template('pay_it_back.html.j2',
                           favors=favors,
                           helper_count=len(helpers),
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)


# Chapter 2, Design Claim 13: "Providing members with specific and highly
# challenging goals, whether self-set or system-suggested, increases contribution."
# Supported by Chapter 2 Design Claims 14 (goals with deadlines) and
# 15 (frequent feedback about performance towards the goal).

# Users set a weekly goal for posts and comments. The system also suggests a
# challenging goal. Progress is shown with progress bars on the goals page
# and on the front page, together with the deadline (end of the week) and a
# comparison to other active members.

from datetime import timedelta

# Minimum goals the system suggests, so that the suggestion is always a challenge
MIN_SUGGESTED_POSTS = 3
MIN_SUGGESTED_COMMENTS = 5


def init_goals_table():
    """Creates the 'goals' table if it does not exist yet."""
    db = sqlite3.connect(DATABASE)
    db.execute('''
        CREATE TABLE IF NOT EXISTS goals (
            user_id      INTEGER PRIMARY KEY,
            post_goal    INTEGER NOT NULL,
            comment_goal INTEGER NOT NULL
        )
    ''')
    db.commit()
    db.close()


# Create the table when the app starts
init_goals_table()


def get_week_start():
    """Returns the start of the current week (Monday 00:00, UTC) and days left.
    UTC is used because the database stores timestamps in UTC."""
    now = datetime.utcnow()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    days_left = (week_end - now).days + 1  # +1 so that Sunday shows "1 day left"
    return week_start, days_left


def count_this_week(table, user_id, week_start):
    """Counts the user's posts or comments created since the start of the week."""
    row = query_db(f'SELECT COUNT(*) AS cnt FROM {table} WHERE user_id = ? AND created_at >= ?',
                   (user_id, week_start.strftime('%Y-%m-%d %H:%M:%S')), one=True)
    return row['cnt']


def suggest_goal(user_id):
    """Suggests a challenging goal: 50 % more than the user's best week so far,
    but never less than the minimum values."""
    best_posts = query_db('''
        SELECT MAX(cnt) AS best FROM (
            SELECT COUNT(*) AS cnt FROM posts WHERE user_id = ?
            GROUP BY strftime('%Y-%W', created_at))
    ''', (user_id,), one=True)['best'] or 0
    best_comments = query_db('''
        SELECT MAX(cnt) AS best FROM (
            SELECT COUNT(*) AS cnt FROM comments WHERE user_id = ?
            GROUP BY strftime('%Y-%W', created_at))
    ''', (user_id,), one=True)['best'] or 0
    return {
        'posts': max(MIN_SUGGESTED_POSTS, round(best_posts * 1.5)),
        'comments': max(MIN_SUGGESTED_COMMENTS, round(best_comments * 1.5)),
    }


@app.template_global()
def get_goal_progress(user_id):
    """Returns the user's goal and progress for this week, or None if the user
    has not set a goal. Registered as a template global so the front page
    can show the progress widget without changing the feed() route."""
    if not user_id:
        return None
    goal = query_db('SELECT post_goal, comment_goal FROM goals WHERE user_id = ?', (user_id,), one=True)
    if not goal:
        return None

    week_start, days_left = get_week_start()
    posts_done = count_this_week('posts', user_id, week_start)
    comments_done = count_this_week('comments', user_id, week_start)

    def percent(done, target):
        # Progress bar width, capped at 100 %
        return min(100, round(done / target * 100)) if target else 100

    return {
        'post_goal': goal['post_goal'],
        'comment_goal': goal['comment_goal'],
        'posts_done': posts_done,
        'comments_done': comments_done,
        'posts_percent': percent(posts_done, goal['post_goal']),
        'comments_percent': percent(comments_done, goal['comment_goal']),
        'completed': posts_done >= goal['post_goal'] and comments_done >= goal['comment_goal'],
        'days_left': days_left,
    }


@app.route('/goals', methods=['GET', 'POST'])
def goals():
    """Page where the user sets a weekly goal and sees their progress."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to set goals.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        # Read and validate the goal values from the form
        try:
            post_goal = int(request.form.get('post_goal', 0))
            comment_goal = int(request.form.get('comment_goal', 0))
        except ValueError:
            flash('Goals must be numbers.', 'warning')
            return redirect(url_for('goals'))

        if not (1 <= post_goal <= 50 and 1 <= comment_goal <= 100):
            flash('Please choose goals between 1-50 posts and 1-100 comments.', 'warning')
            return redirect(url_for('goals'))

        # INSERT OR REPLACE: creates the goal, or updates it if it already exists
        db = get_db()
        db.execute('INSERT OR REPLACE INTO goals (user_id, post_goal, comment_goal) VALUES (?, ?, ?)',
                   (user_id, post_goal, comment_goal))
        db.commit()
        flash('Your weekly goal has been saved. Good luck!', 'success')
        return redirect(url_for('goals'))

    # Comparison with other members: average posts/comments this week
    # among users who have been active this week (excluding the current user)
    week_start, _ = get_week_start()
    week_start_str = week_start.strftime('%Y-%m-%d %H:%M:%S')
    community = query_db('''
        SELECT COUNT(DISTINCT user_id) AS active_users,
               SUM(is_post) AS posts, SUM(1 - is_post) AS comments
        FROM (
            SELECT user_id, 1 AS is_post FROM posts WHERE created_at >= ? AND user_id != ?
            UNION ALL
            SELECT user_id, 0 AS is_post FROM comments WHERE created_at >= ? AND user_id != ?
        )
    ''', (week_start_str, user_id, week_start_str, user_id), one=True)

    community_avg = None
    if community['active_users']:
        community_avg = {
            'users': community['active_users'],
            'posts': round(community['posts'] / community['active_users'], 1),
            'comments': round(community['comments'] / community['active_users'], 1),
        }

    return render_template('goals.html.j2',
                           progress=get_goal_progress(user_id),
                           suggestion=suggest_goal(user_id),
                           community_avg=community_avg)






# Coding Assignment #2

# Shared helpers for content moderation (Assignment 2.1) and risk analysis
# (Assignment 2.2). The rules implemented here follow the specification on
# the /rules page (templates/rules.html.j2).


# Score constants from the rules page, kept in one place so they are easy to check.
SEVERE_VIOLATION_SCORE = 5.0      # Rules 1.1.1 and 1.1.2
TIER3_WORD_SCORE = 2.0            # Rule 1.2.1, per match
EXTERNAL_LINK_SCORE = 2.0         # Rule 1.2.2, per match
EXCESSIVE_CAPS_SCORE = 0.5        # Rule 1.2.3, fixed
CAPS_MIN_LETTERS = 15             # Rule 1.2.3: "more than 15 alphabetic characters"
CAPS_UPPERCASE_RATIO = 0.70       # Rule 1.2.3: "more than 70% uppercase"

SEVERE_REMOVAL_TEXT = '[content removed due to severe violation]'
SPAM_REMOVAL_TEXT = '[content removed due to spam/scam policy]'
LINK_REPLACEMENT_TEXT = '[link removed]'


def build_whole_word_pattern(terms):
    """
    Builds one compiled, case-insensitive regex that matches any of the given
    words/phrases as a *whole* word or phrase.

    - re.escape() makes sure special characters in the word list are treated literally.
    - Longer terms are tried first, so a phrase wins over a shorter word inside it.
    - (?<!\\w) and (?!\\w) are used instead of \\b, because \\b does not work when a
      term starts or ends with a non-letter character (e.g. "$$$").
    Compiling one pattern per tier once at start-up is much faster than looping over
    every word for every post, which matters on the admin page that scores all content.
    """
    escaped = sorted((re.escape(t) for t in terms if t), key=len, reverse=True)
    return re.compile(r'(?<!\w)(?:' + '|'.join(escaped) + r')(?!\w)', re.IGNORECASE)


TIER1_PATTERN = build_whole_word_pattern(TIER1_WORDS)
TIER2_PATTERN = build_whole_word_pattern(TIER2_PHRASES)
TIER3_PATTERN = build_whole_word_pattern(TIER3_WORDS)

# Rule 1.2.2: an external link is anything starting with http(s):// or www.
# The match continues until the next whitespace character.
URL_PATTERN = re.compile(r'(?:https?://|www\.)\S+', re.IGNORECASE)


def analyze_content(content):
    """
    Runs all moderation rules on a piece of text and explains what was found.

    This is the "engine" behind moderate_content(). It returns more information
    than moderate_content() needs, because the Design Claim 20 feature (reminder
    at the point of action) has to tell the user *which* rule their text breaks.

    Returns a dict:
        moderated: the filtered text (what other users would see)
        score:     the Content Score (float)
        removed:   True if the whole text would be removed (Stage 1.1)
        violations: list of dicts {rule, title, detail, points} - one per rule broken
    """
    result = {'moderated': content or '', 'score': 0.0, 'removed': False, 'violations': []}
    if not content:
        return result

    # --- Stage 1.1: severe violations. The first match ends the check. ---

    # Rule 1.1.1: Tier 1 words -> whole content removed, fixed score 5.0
    match = TIER1_PATTERN.search(content)
    if match:
        result.update(moderated=SEVERE_REMOVAL_TEXT, score=SEVERE_VIOLATION_SCORE, removed=True)
        result['violations'].append({
            'rule': '1.1.1', 'title': 'Severe language',
            'detail': f'"{match.group(0)}" is not allowed on MiniSocial. The whole text would be removed.',
            'points': SEVERE_VIOLATION_SCORE})
        return result

    # Rule 1.1.2: Tier 2 spam/scam phrases -> whole content removed, fixed score 5.0
    match = TIER2_PATTERN.search(content)
    if match:
        result.update(moderated=SPAM_REMOVAL_TEXT, score=SEVERE_VIOLATION_SCORE, removed=True)
        result['violations'].append({
            'rule': '1.1.2', 'title': 'Spam or scam phrase',
            'detail': f'"{match.group(0)}" looks like spam. The whole text would be removed.',
            'points': SEVERE_VIOLATION_SCORE})
        return result

    # --- Stage 1.2: scored violations. Score starts at 0.0. ---
    moderated = content
    score = 0.0

    # Rule 1.2.1: each Tier 3 word is replaced by asterisks of the same length, +2.0 each
    found_words = TIER3_PATTERN.findall(moderated)
    if found_words:
        moderated = TIER3_PATTERN.sub(lambda m: '*' * len(m.group(0)), moderated)
        points = TIER3_WORD_SCORE * len(found_words)
        score += points
        result['violations'].append({
            'rule': '1.2.1', 'title': 'Mild profanity',
            'detail': f'{len(found_words)} word(s) would be hidden with asterisks: '
                      + ', '.join(f'"{w}"' for w in found_words),
            'points': points})

    # Rule 1.2.2: each URL is replaced with "[link removed]", +2.0 each
    found_links = URL_PATTERN.findall(moderated)
    if found_links:
        moderated = URL_PATTERN.sub(LINK_REPLACEMENT_TEXT, moderated)
        points = EXTERNAL_LINK_SCORE * len(found_links)
        score += points
        result['violations'].append({
            'rule': '1.2.2', 'title': 'External link',
            'detail': f'{len(found_links)} link(s) would be shown as {LINK_REPLACEMENT_TEXT}.',
            'points': points})

    # Rule 1.2.3: >15 letters and >70 % of them uppercase -> +0.5, text is NOT modified.
    # Counted from the original content so that masking does not change the result.
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) > CAPS_MIN_LETTERS:
        upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
        if upper_ratio > CAPS_UPPERCASE_RATIO:
            score += EXCESSIVE_CAPS_SCORE
            result['violations'].append({
                'rule': '1.2.3', 'title': 'Excessive capitalization',
                'detail': f'{round(upper_ratio * 100)} % of the letters are capitals. '
                          'Writing in capitals reads as shouting.',
                'points': EXCESSIVE_CAPS_SCORE})

    result['moderated'] = moderated
    result['score'] = score
    return result


def account_age_days(created_at):
    """
    Returns the account age in whole days. created_at can be a datetime (the
    normal case, thanks to PARSE_DECLTYPES) or a string. Returns None if unknown.
    UTC is used because SQLite's CURRENT_TIMESTAMP is in UTC.
    """
    if created_at is None:
        return None
    if isinstance(created_at, str):
        try:
            created_at = datetime.strptime(created_at[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return None
    return (datetime.utcnow() - created_at).days


def get_risk_label(score):
    """Translates a risk score to a label using the thresholds on the rules page."""
    if score >= 5.0:
        return 'HIGH'
    if score >= 3.0:
        return 'MEDIUM'
    if score >= 1.0:
        return 'LOW'
    return 'NONE'


# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float number score showing the risk associated with this user. There are no strict rules or bounds to this score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk. (An upper bound of 5.0 is applied to this score elsewhere in the codebase) 
        
        You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
        Then, navigate to the /admin endpoint. (http://localhost:8080/admin)

    Implementation follows Rule 2.2 on the /rules page.
    """
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return 0.0

    # Step 1: Content Score of the profile text
    _, profile_score = moderate_content(user['profile'] or '')

    # Step 2: average Content Score of all posts (0 if the user has no posts)
    post_rows = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    post_scores = [moderate_content(row['content'])[1] for row in post_rows]
    average_post_score = sum(post_scores) / len(post_scores) if post_scores else 0.0

    # Step 3: average Content Score of all comments (0 if the user has no comments)
    comment_rows = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    comment_scores = [moderate_content(row['content'])[1] for row in comment_rows]
    average_comment_score = sum(comment_scores) / len(comment_scores) if comment_scores else 0.0

    # Step 4: weighted combination - posts are the most visible content, so they weigh 3x
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

    # Step 5: account age multiplier - new accounts are treated as riskier
    age = account_age_days(user['created_at'])
    if age is not None and age < 7:
        user_risk_score = content_risk_score * 1.5
    elif age is not None and age < 30:
        user_risk_score = content_risk_score * 1.2
    else:
        user_risk_score = content_risk_score

    # Step 6: cap the final score at 5.0
    return min(user_risk_score, 5.0)

    
# Assignment 2.1
def moderate_content(content):
    """
    Args
        content: the text content of a post or comment to be moderated.
        
    Returns: 
        A tuple containing the moderated content (string) and a severity score (float). There are no strict rules or bounds to the severity score, other than that a score of less than 1.0 means no risk, 1.0 to 3.0 is low risk, 3.0 to 5.0 is medium risk and above 5.0 is high risk.
    
    This function moderates a string of content and calculates a severity score based on
    rules loaded from the 'censorship.dat' file. These are already loaded as TIER1_WORDS, TIER2_PHRASES and TIER3_WORDS. Tier 1 corresponds to strong profanity, Tier 2 to scam/spam phrases and Tier 3 to mild profanity.
    
    You will be able to check the scores by logging in with the administrator account:
            username: admin
            password: admin
    Then, navigate to the /admin endpoint. (http://localhost:8080/admin)

    Implementation: all rules (Part 1 of the /rules page) are applied in
    analyze_content() above; this function only returns the two values the rest
    of the codebase expects.
    """
    analysis = analyze_content(content)
    moderated_content = analysis['moderated']
    score = analysis['score']
    
    return moderated_content, score


# Coding Assignment #2
#
# Design Claim 20 ch. 4:
#   "Offering people reminders at the point of an action that may violate norms
#    reduces the number of offenses."
#

#   1. While the user types a post or comment, the browser asks /moderation/check
#      whether the text breaks a rule and shows a small live hint under the text box
#      (static/moderation_reminder.js).
#   2. When the user submits text that breaks a rule, it is NOT published right away.
#      Instead a reminder page explains which rule is broken (with a link to the rule),
#      shows how others would see the text and what risk score it would get, and lets
#      the user edit the text, cancel, or post anyway. Text that would be removed
#      completely (Tier 1 / Tier 2) cannot be posted anyway.
#   3. Every reminder and what the user did after it is stored in the
#      moderation_prompts table, and the admin dashboard shows how often reminders
#      led to an edited (or cancelled) post. This lets us check whether the
#      reminders actually reduce offenses, which is what the design claim predicts.

# Possible outcomes of a reminder
REMINDER_PENDING = 'pending'            # reminder shown, user has not decided yet
REMINDER_REVISED = 'revised'            # user edited the text and posted a less risky version
REMINDER_POSTED_ANYWAY = 'posted_anyway'  # user posted without lowering the score
REMINDER_CANCELLED = 'cancelled'        # user decided not to post at all


def init_moderation_prompts_table():
    """Creates the table that logs the Design Claim 20 reminders, if it does not exist."""
    db = sqlite3.connect(DATABASE)
    db.execute('''
        CREATE TABLE IF NOT EXISTS moderation_prompts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            content_type  TEXT    NOT NULL,           -- 'post' or 'comment'
            post_id       INTEGER,                    -- the commented post (comments only)
            first_content TEXT    NOT NULL,           -- the text that triggered the reminder
            first_score   REAL    NOT NULL,           -- its Content Score
            final_score   REAL,                       -- Content Score of what was finally posted
            outcome       TEXT    NOT NULL DEFAULT 'pending',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
            resolved_at   TIMESTAMP
        )
    ''')
    db.commit()
    db.close()


# Create the table when the app starts
init_moderation_prompts_table()


def get_pending_prompt(user_id):
    """
    Returns the reminder row this form submission belongs to (the hidden prompt_id
    field on the reminder page), or None. Only the user's own, still pending
    reminders are accepted, so a user cannot change someone else's statistics.
    """
    prompt_id = request.form.get('prompt_id', type=int)
    if not prompt_id:
        return None
    return query_db('SELECT * FROM moderation_prompts WHERE id = ? AND user_id = ? AND outcome = ?',
                    (prompt_id, user_id, REMINDER_PENDING), one=True)


def check_before_publishing(content_type, content, post_id=None):
    """
    Checks the content before publishing and shows a reminder if necessary.

    Called by add_post() and add_comment() before anything is saved.
    Returns None if the content can be published, or a rendered reminder page if the
    user should first see the reminder.

    Content can be published when:
      - it breaks no rules, or
      - the user has already seen the reminder and pressed "Post anyway"
        (only allowed when the content would not be removed completely).
    """
    user_id = session.get('user_id')
    analysis = analyze_content(content)
    confirmed = request.form.get('confirmed') == '1'
    prompt = get_pending_prompt(user_id)

    if not analysis['violations']:
        return None

    reviewed = (request.form.get('reviewed_content') or '').replace('\r\n', '\n')
    same_text = reviewed == content.replace('\r\n', '\n')
    if confirmed and prompt and same_text and not analysis['removed']:
        return None

    # The reminder is needed. Create a log row the first time it is shown for this
    # draft; if the user edits and the text still breaks a rule, the same row is reused.
    if not prompt:
        cur = query_db('''INSERT INTO moderation_prompts
                            (user_id, content_type, post_id, first_content, first_score)
                          VALUES (?, ?, ?, ?, ?)''',
                       (user_id, content_type, post_id, content, analysis['score']), commit=True)
        prompt_id = cur.lastrowid
    else:
        prompt_id = prompt['id']

    # Show the same risk score the admin would see: new accounts (< 7 days) get x1.5 (Rule 2.1)
    author = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
    age = account_age_days(author['created_at']) if author else None
    is_new_account = age is not None and age < 7
    risk_score = analysis['score'] * (1.5 if is_new_account else 1.0)

    return render_template('moderation_reminder.html.j2',
                           content_type=content_type,
                           content=content,
                           post_id=post_id,
                           prompt_id=prompt_id,
                           analysis=analysis,
                           risk_score=round(risk_score, 2),
                           risk_label=get_risk_label(risk_score),
                           is_new_account=is_new_account)


def resolve_reminder(content):
    """
    Called right after a post/comment has been saved. If the user came through the
    reminder page, store what they finally did: lowered the score by editing
    (revised) or posted without lowering it (posted anyway).
    """
    prompt = get_pending_prompt(session.get('user_id'))
    if not prompt:
        return
    _, final_score = moderate_content(content)
    outcome = REMINDER_REVISED if final_score < prompt['first_score'] else REMINDER_POSTED_ANYWAY
    query_db('''UPDATE moderation_prompts
                SET final_score = ?, outcome = ?, resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?''', (final_score, outcome, prompt['id']), commit=True)


@app.route('/reminders/<int:prompt_id>/cancel', methods=['POST'])
def cancel_reminder(prompt_id):
    """The user decided not to post after seeing the reminder."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))

    prompt = query_db('SELECT * FROM moderation_prompts WHERE id = ? AND user_id = ? AND outcome = ?',
                      (prompt_id, user_id, REMINDER_PENDING), one=True)
    if prompt:
        query_db('''UPDATE moderation_prompts
                    SET outcome = ?, resolved_at = CURRENT_TIMESTAMP WHERE id = ?''',
                 (REMINDER_CANCELLED, prompt_id), commit=True)
        flash('Your text was not posted. Thanks for keeping MiniSocial friendly!', 'info')

    # Send the user back to where they were writing
    if prompt and prompt['content_type'] == 'comment' and prompt['post_id']:
        return redirect(url_for('post_detail', post_id=prompt['post_id']))
    return redirect(url_for('feed'))


@app.route('/moderation/check', methods=['POST'])
def moderation_check():
    """
    JSON endpoint for the live hint shown while typing (static/moderation_reminder.js).
    Returns the rule check result for the given text. Nothing is saved.
    """
    if not session.get('user_id'):
        return {'error': 'login required'}, 401
    data = request.get_json(silent=True) or {}
    analysis = analyze_content(str(data.get('content', ''))[:5000])  # limit size of the check
    return {
        'score': analysis['score'],
        'removed': analysis['removed'],
        'preview': analysis['moderated'],
        'violations': [{'rule': v['rule'], 'title': v['title']} for v in analysis['violations']],
    }


def get_reminder_stats():
    """
    Statistics for the admin dashboard: how did users react to the reminders?
    'Offenses avoided' = reminders after which the user either edited the text to a
    lower score or did not post at all.
    """
    rows = query_db('SELECT outcome, COUNT(*) AS cnt FROM moderation_prompts GROUP BY outcome')
    counts = {REMINDER_PENDING: 0, REMINDER_REVISED: 0, REMINDER_POSTED_ANYWAY: 0, REMINDER_CANCELLED: 0}
    for row in rows or []:
        counts[row['outcome']] = row['cnt']

    resolved = counts[REMINDER_REVISED] + counts[REMINDER_POSTED_ANYWAY] + counts[REMINDER_CANCELLED]
    avoided = counts[REMINDER_REVISED] + counts[REMINDER_CANCELLED]

    scores = query_db('''SELECT AVG(first_score) AS first_avg, AVG(final_score) AS final_avg
                         FROM moderation_prompts WHERE outcome IN (?, ?)''',
                      (REMINDER_REVISED, REMINDER_POSTED_ANYWAY), one=True)
    return {
        'total': sum(counts.values()),
        'counts': counts,
        'resolved': resolved,
        'avoided': avoided,
        'avoided_percent': round(avoided / resolved * 100) if resolved else None,
        'first_avg': round(scores['first_avg'], 2) if scores and scores['first_avg'] is not None else None,
        'final_avg': round(scores['final_avg'], 2) if scores and scores['final_avg'] is not None else None,
    }


@app.route('/admin/delete/reminder/<int:prompt_id>', methods=['POST'])
def admin_delete_reminder(prompt_id):
    """Admin: delete one row from the reminder log (e.g. test data)."""
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
    query_db('DELETE FROM moderation_prompts WHERE id = ?', (prompt_id,), commit=True)
    flash(f'Reminder {prompt_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard', tab='reminders'))


@app.route('/admin/reminders/clear', methods=['POST'])
def admin_clear_reminders():
    """Admin: empty the whole reminder log, so the statistics start from zero."""
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
    query_db('DELETE FROM moderation_prompts', commit=True)
    flash('All reminders have been deleted.', 'success')
    return redirect(url_for('admin_dashboard', tab='reminders'))


def get_recent_reminders(limit=25):
    """Latest reminders for the admin dashboard. The ORIGINAL (unfiltered) text is
    shown, like on the Posts and Comments tabs, so the moderator can see exactly
    what triggered the reminder."""
    rows = query_db('''SELECT m.*, u.username FROM moderation_prompts m
                       JOIN users u ON m.user_id = u.id
                       ORDER BY m.id DESC LIMIT ?''', (limit,))
    return [dict(row) for row in rows or []]


# Coding Assignment #3
# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook

    Implementation: content-based filtering with a bonus for followed users.
    See the helper functions and constants right below this function.
    """
    if not user_id:
        # Not logged in: we know nothing about the visitor, show popular posts
        return popular_fallback(None, filter_following=False)

    all_posts = query_db('''SELECT p.id, p.content, p.created_at, p.user_id, u.username
                          FROM posts p JOIN users u ON p.user_id = u.id''')
    followed_ids = {row['followed_id'] for row in
                    query_db('SELECT followed_id FROM follows WHERE follower_id = ?', (user_id,))}
    reacted_rows = query_db('SELECT post_id, reaction_type FROM reactions WHERE user_id = ?', (user_id,))
    reacted_ids = {row['post_id'] for row in reacted_rows}
    liked_ids = {row['post_id'] for row in reacted_rows if row['reaction_type'] in POSITIVE_REACTIONS}

    # Step 1: turn every post into a set of words and compute how rare each word is (IDF)
    tokens = {post['id']: tokenize(post['content']) for post in all_posts}
    idf = compute_idf(tokens.values())

    # Step 2: build the user's interest profile (word -> weight)
    interest = collections.Counter()
    for post_id in liked_ids:                       # posts the user reacted to positively
        for word in tokens.get(post_id, ()):
            interest[word] += LIKED_POST_WEIGHT * idf[word]

    posts_by_author = collections.defaultdict(list)
    for post in all_posts:
        posts_by_author[post['user_id']].append(post['id'])
    for author_id in followed_ids:                  # posts of the users the user follows
        author_posts = posts_by_author.get(author_id, [])
        for post_id in author_posts:
            # Each followed user adds FOLLOWED_USER_WEIGHT in total, spread over their posts,
            # so a followed user who posts a lot does not take over the whole profile.
            for word in tokens[post_id]:
                interest[word] += FOLLOWED_USER_WEIGHT * idf[word] / len(author_posts)

    # Cold start: a brand-new user has not reacted to or followed anything yet
    if not interest:
        return popular_fallback(user_id, filter_following)

    # Step 3: score candidate posts by cosine similarity to the interest profile
    profile_norm = math.sqrt(sum(w * w for w in interest.values()))
    scored = []
    for post in all_posts:
        if post['user_id'] == user_id or post['id'] in reacted_ids:
            continue                                # skip own posts and already seen/reacted ones
        if filter_following and post['user_id'] not in followed_ids:
            continue                                # "Following" filter
        words = tokens[post['id']]
        if not words:
            continue
        overlap = [w for w in words if w in interest]
        if not overlap and post['user_id'] not in followed_ids:
            continue
        dot = sum(interest[w] * idf[w] for w in overlap)
        post_norm = math.sqrt(sum(idf[w] ** 2 for w in words))
        similarity = dot / (profile_norm * post_norm) if post_norm else 0.0
        is_followed = post['user_id'] in followed_ids
        score = similarity + (FOLLOW_BONUS if is_followed else 0.0)
        scored.append((score, post, overlap, is_followed))

    scored.sort(key=lambda item: item[0], reverse=True)

    # Step 4: take the best posts, but skip content the moderation system would remove
    # (Coding Assignment 2) so spam and severe violations are never recommended.
    recommended_posts = []
    for score, post, overlap, is_followed in scored:
        if moderate_content(post['content'])[1] >= SEVERE_VIOLATION_SCORE:
            continue
        item = dict(post)
        item['reason'] = recommendation_reason(overlap, interest, is_followed)
        recommended_posts.append(item)
        if len(recommended_posts) == RECOMMENDATION_COUNT:
            break

    # The docstring asks for reverse-chronological order
    recommended_posts.sort(key=lambda p: str(p['created_at']), reverse=True)
    return recommended_posts



# Helpers for recommend()
# Algorithm: content-based filtering (TF-IDF + cosine similarity).
#   - Every post is represented by its words and hashtags.
#   - Words that appear in many posts ("today", "really") say little about a topic,
#     so each word is weighted by its inverse document frequency (IDF).
#   - The user's interest profile is the weighted sum of the words in the posts they
#     reacted to positively, plus (with a smaller weight) the posts of the users they follow.
#   - Each unseen post gets a score = cosine similarity to the profile, plus a small
#     bonus if its author is followed. The 5 best posts are recommended.
RECOMMENDATION_COUNT = 5
POSITIVE_REACTIONS = {'like', 'love', 'laugh', 'haha', 'wow'}  # 'sad' and 'angry' are not "liking"
LIKED_POST_WEIGHT = 1.0
FOLLOWED_USER_WEIGHT = 0.5
FOLLOW_BONUS = 0.05

# Very common English words that do not tell anything about the topic of a post
STOPWORDS = set('''
a about after again all also am an and any are as at be because been before being but by can
could did do does doing don done down even ever every feel for from get gets getting go going
got had has have having he her here him his how i if im in into is it its ive just know last
like lot made make me more most much my need new no not now of off on one only or other our out
over really right see she should so some still such than that thats the their them then there
these they thing things think this those through time to today too up us very was way we well
were what when where which while who why will with would yes yet you your youre
finally finished amazing great love day days weekend night better best expected tried try
'''.split())

WORD_PATTERN = re.compile(r"#?[a-z][a-z0-9']{2,}")


def tokenize(text):
    """Lower-cases the text and returns the set of meaningful words.
    Hashtags are treated as normal words ("#DIY" -> "diy") so they match the text too."""
    words = set()
    for raw in WORD_PATTERN.findall((text or '').lower()):
        word = raw.lstrip('#').replace("'", '')
        if word not in STOPWORDS and len(word) > 2:
            words.add(word)
    return words


def compute_idf(token_sets):
    """Inverse document frequency: rare words get a high weight, common words a low one."""
    token_sets = list(token_sets)
    doc_count = collections.Counter(word for words in token_sets for word in words)
    total = len(token_sets)
    return collections.defaultdict(
        lambda: math.log(total + 1),
        {word: math.log((total + 1) / (count + 1)) + 1 for word, count in doc_count.items()})


def recommendation_reason(overlap, interest, is_followed):
    """A short explanation shown under a recommended post in the feed."""
    top_words = sorted(overlap, key=lambda w: interest[w], reverse=True)[:3]
    parts = []
    if top_words:
        parts.append('you liked posts about ' + ', '.join(top_words))
    if is_followed:
        parts.append('you follow this user')
    return 'Recommended because ' + ' and '.join(parts) if parts else None


def popular_fallback(user_id, filter_following):
    """
    Cold start: when we know nothing about the user yet (a newcomer who has not
    reacted to or followed anything), recommend the most-reacted posts instead of
    showing an empty page.
    """
    params = []
    conditions = []
    if user_id:
        conditions.append('p.user_id != ?')
        params.append(user_id)
        if filter_following:
            conditions.append('p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)')
            params.append(user_id)
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

    rows = query_db(f'''
        SELECT p.id, p.content, p.created_at, p.user_id, u.username,
               COUNT(r.id) AS reaction_count
        FROM posts p
        JOIN users u ON p.user_id = u.id
        LEFT JOIN reactions r ON r.post_id = p.id
        {where}
        GROUP BY p.id
        ORDER BY reaction_count DESC, p.created_at DESC
        LIMIT 50''', params)

    posts = []
    for row in rows:
        if moderate_content(row['content'])[1] > 0:
            continue                                # show newcomers only content that follows the rules
        item = dict(row)
        item['reason'] = 'Popular in the community - react to posts you like to get personal recommendations'
        posts.append(item)
        if len(posts) == RECOMMENDATION_COUNT:
            break
    posts.sort(key=lambda p: str(p['created_at']), reverse=True)
    return posts


# Coding Assignment #3: two Design Claims from chapter 5

NEWCOMER_PERIOD_DAYS = 7        # after this the account is a full member anyway
NEWCOMER_DAILY_POST_LIMIT = 3   # posts per 24 hours while still a newcomer
PROFILE_MIN_LENGTH = 20         # characters needed for the "tell us about yourself" step
FOLLOW_GOAL = 3
REACTION_GOAL = 3


def init_onboarding_table():
    """Stores the parts of the Newcomer Path that cannot be read from other tables."""
    db = sqlite3.connect(DATABASE)
    db.execute('''
        CREATE TABLE IF NOT EXISTS onboarding (
            user_id       INTEGER PRIMARY KEY,
            rules_read_at TIMESTAMP,   -- when the user confirmed reading the rules (step 1)
            completed_at  TIMESTAMP    -- when the user finished the whole path ("graduated")
        )
    ''')
    db.commit()
    db.close()


init_onboarding_table()


def get_onboarding_status(user_id):
    """
    Returns everything about the user's place on the Newcomer Path:
    the six steps (in order), which step is next, whether the user is still a
    newcomer, how many days of the newcomer period are left and which privileges
    are unlocked. Progress is read from the real data (follows, reactions, ...),
    so the user learns the platform by actually using it.
    """
    user = query_db('SELECT id, username, profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if not user:
        return None
    row = query_db('SELECT rules_read_at, completed_at FROM onboarding WHERE user_id = ?', (user_id,), one=True)

    def count(sql):
        return query_db(sql, (user_id,), one=True)['cnt']

    follows = count('SELECT COUNT(*) AS cnt FROM follows WHERE follower_id = ?')
    reactions = count('SELECT COUNT(*) AS cnt FROM reactions WHERE user_id = ?')
    comments = count('SELECT COUNT(*) AS cnt FROM comments WHERE user_id = ?')
    posts = count('SELECT COUNT(*) AS cnt FROM posts WHERE user_id = ?')
    profile_length = len((user['profile'] or '').strip())

    # The six steps, always in this order 
    # progress is shown to the user
    steps = [
        {'key': 'rules', 'title': 'Read the community rules',
         'text': 'Learn what is OK on MiniSocial before you post. This unlocks posting.',
         'done': bool(row and row['rules_read_at']), 'progress': None,
         'link': url_for('rules') + '#newcomer-confirm', 'link_label': 'Read the rules'},
        {'key': 'profile', 'title': 'Tell others about yourself',
         'text': f'Write a short profile (at least {PROFILE_MIN_LENGTH} characters) so others know who you are.',
         'done': profile_length >= PROFILE_MIN_LENGTH,
         'progress': f'{min(profile_length, PROFILE_MIN_LENGTH)} / {PROFILE_MIN_LENGTH} characters',
         'link': url_for('newcomer_path') + '#profile-form', 'link_label': 'Write your profile'},
        {'key': 'follow', 'title': f'Follow {FOLLOW_GOAL} members',
         'text': 'Find people you find interesting. Their posts will appear in your Following feed.',
         'done': follows >= FOLLOW_GOAL, 'progress': f'{min(follows, FOLLOW_GOAL)} / {FOLLOW_GOAL} followed',
         'link': url_for('newcomer_path') + '#suggested-members', 'link_label': 'See suggested members'},
        {'key': 'react', 'title': f'React to {REACTION_GOAL} posts',
         'text': 'Reactions are the easiest way to take part. They also teach the Recommended tab what you like.',
         'done': reactions >= REACTION_GOAL, 'progress': f'{min(reactions, REACTION_GOAL)} / {REACTION_GOAL} reactions',
         'link': url_for('feed', sort='popular'), 'link_label': 'Browse popular posts'},
        {'key': 'comment', 'title': 'Write your first comment',
         'text': 'Join a conversation by commenting on a post you like.',
         'done': comments >= 1, 'progress': None,
         'link': url_for('feed', sort='popular'), 'link_label': 'Find a post to comment on'},
        {'key': 'post', 'title': 'Write your first post',
         'text': 'Introduce yourself or share something you care about.',
         'done': posts >= 1, 'progress': None,
         'link': url_for('feed'), 'link_label': 'Write a post'},
    ]

    # a step only counts as completed if all earlier steps are completed,
    # only the first unfinished step is "current" rest are shown as locked.
    current_index = None
    for i, step in enumerate(steps):
        if current_index is not None:
            step['state'] = 'locked'
        elif step['done']:
            step['state'] = 'done'
        else:
            step['state'] = 'current'
            current_index = i
    completed_steps = current_index if current_index is not None else len(steps)
    path_finished = current_index is None

    # Mark the moment of "graduation" once, so it can be shown and is not lost
    # if the user later e.g. unfollows someone.
    completed_at = row['completed_at'] if row else None
    if path_finished and not completed_at:
        query_db('''INSERT INTO onboarding (user_id, completed_at) VALUES (?, CURRENT_TIMESTAMP)
                    ON CONFLICT(user_id) DO UPDATE SET completed_at = CURRENT_TIMESTAMP''',
                 (user_id,), commit=True)
        completed_at = datetime.utcnow()

    # Fixed timetable (DC 22) and progressive access (DC 25)
    age = account_age_days(user['created_at'])
    age = age if age is not None else NEWCOMER_PERIOD_DAYS
    is_newcomer = not completed_at and age < NEWCOMER_PERIOD_DAYS
    days_left = max(0, NEWCOMER_PERIOD_DAYS - age)

    rules_read = steps[0]['done']
    privileges = [
        {'name': 'React to posts', 'unlocked': True, 'how': 'Always allowed'},
        {'name': 'Comment on posts', 'unlocked': True, 'how': 'Always allowed'},
        {'name': 'Create posts', 'unlocked': rules_read or not is_newcomer,
         'how': 'Unlocks after step 1: reading the rules'},
        {'name': 'Unlimited posting', 'unlocked': not is_newcomer,
         'how': f'Newcomers can post {NEWCOMER_DAILY_POST_LIMIT} posts per day. Unlocks when you finish the path'},
    ]

    return {
        'steps': steps,
        'completed_steps': completed_steps,
        'total_steps': len(steps),
        'percent': round(completed_steps / len(steps) * 100),
        'current_step': steps[current_index] if current_index is not None else None,
        'path_finished': path_finished,
        'completed_at': completed_at,
        'is_newcomer': is_newcomer,
        'account_age_days': age,
        'days_left': days_left,
        'can_post': rules_read or not is_newcomer,
        'privileges': privileges,
    }


@app.template_global()
def newcomer_status():
    """Template helper: the logged-in user's onboarding status, or None.
    Used by the feed banner and the navigation menu."""
    user_id = session.get('user_id')
    if not user_id:
        return None
    # Cache for the duration of one request (the menu and the feed both use it)
    if 'newcomer_status' not in g:
        g.newcomer_status = get_onboarding_status(user_id)
    return g.newcomer_status


def check_newcomer_access(content_type):
    """
    Design Claim 25: progressive access control. Called by add_post()
    BEFORE anything is saved. Reacting and commenting are always allowed, so only
    creating posts is checked. Returns an explanation (string) if the action is not yet
    allowed for this newcomer, or None if it is allowed.
    The message always says what unlocks the privilege, so the limit feels like a
    step on the path, not a punishment.
    """
    status = get_onboarding_status(session.get('user_id'))
    if not status or not status['is_newcomer']:
        return None                                   # full members have no limits

    if content_type == 'post':
        if not status['can_post']:
            return 'New members can create posts after reading the community rules (step 1 of your Newcomer Path).'
        posts_today = query_db('''SELECT COUNT(*) AS cnt FROM posts
                                 WHERE user_id = ? AND created_at >= datetime('now', '-1 day')''',
                              (session.get('user_id'),), one=True)['cnt']
        if posts_today >= NEWCOMER_DAILY_POST_LIMIT:
            return (f'New members can write {NEWCOMER_DAILY_POST_LIMIT} posts per day. '
                    'Finish your Newcomer Path to remove the limit.')
    return None


@app.route('/welcome', methods=['GET'])
def newcomer_path():
    """The Newcomer Path page (Design Claims 22 and 25)."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to see your Newcomer Path.', 'danger')
        return redirect(url_for('login'))

    status = get_onboarding_status(user_id)
    me = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)

    # Suggested members to follow (step 3): active members with the most followers
    # that the user does not follow yet. Their latest post is shown as a preview.
    suggested_raw = query_db('''
        SELECT u.id, u.username, COUNT(f.follower_id) AS followers
        FROM users u
        JOIN follows f ON f.followed_id = u.id
        WHERE u.id != ? AND u.username != 'admin'
          AND u.id NOT IN (SELECT followed_id FROM follows WHERE follower_id = ?)
          AND EXISTS (SELECT 1 FROM posts p WHERE p.user_id = u.id)
        GROUP BY u.id
        ORDER BY followers DESC
        LIMIT 5''', (user_id, user_id))
    suggested = []
    for member in suggested_raw:
        item = dict(member)
        latest = query_db('SELECT content FROM posts WHERE user_id = ? ORDER BY created_at DESC LIMIT 1',
                          (item['id'],), one=True)
        item['latest_post'] = moderate_content(latest['content'])[0] if latest else ''
        suggested.append(item)

    # Collective socialization (DC 22): the user's cohort = other members who joined
    # within NEWCOMER_PERIOD_DAYS of this user. Showing their progress makes the
    # newcomers a group that goes through the same steps together.
    cohort_raw = query_db('''
        SELECT id, username, created_at FROM users
        WHERE id != ? AND username != 'admin'
          AND ABS(julianday(created_at) - julianday(?)) <= ?
        ORDER BY created_at DESC LIMIT 20''',
        (user_id, str(me['created_at']), NEWCOMER_PERIOD_DAYS))
    cohort = []
    for member in cohort_raw:
        member_status = get_onboarding_status(member['id'])
        cohort.append({'id': member['id'], 'username': member['username'],
                       'completed_steps': member_status['completed_steps'],
                       'path_finished': member_status['path_finished']})

    return render_template('newcomer_path.html.j2',
                           status=status,
                           profile=me['profile'] or '',
                           suggested=suggested,
                           cohort=cohort,
                           profile_min_length=PROFILE_MIN_LENGTH)


@app.route('/welcome/rules-read', methods=['POST'])
def confirm_rules_read():
    """Step 1: the user confirms they have read the rules (button on the /rules page)."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    query_db('''INSERT INTO onboarding (user_id, rules_read_at) VALUES (?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET rules_read_at = COALESCE(rules_read_at, CURRENT_TIMESTAMP)''',
             (user_id,), commit=True)
    flash('Thanks for reading the rules! Posting is now unlocked.', 'success')
    return redirect(url_for('newcomer_path'))


@app.route('/welcome/profile', methods=['POST'])
def update_profile():
    """Step 2: write or update the profile text. The text goes through the same
    moderation as everything else (Coding Assignment 2)."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    profile = (request.form.get('profile') or '').strip()

    analysis = analyze_content(profile)
    if analysis['removed']:
        flash('Your profile contains something our rules do not allow. Please edit it.', 'warning')
        return redirect(url_for('newcomer_path') + '#profile-form')
    query_db('UPDATE users SET profile = ? WHERE id = ?', (profile, user_id), commit=True)
    flash('Your profile was saved.', 'success')
    return redirect(url_for('newcomer_path'))


if __name__ == '__main__':
    app.run(debug=True, port=8080)

