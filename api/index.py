# Vercel Configuration for 3D Asset Manager
import sys
import os

# Add the project root to Python path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import create_app
    
    # Create the Flask app instance
    app = create_app()
    
except Exception as e:
    startup_error = str(e)
    print(f"Error creating Flask app: {startup_error}")
    # Fallback minimal app for debugging
    from flask import Flask
    app = Flask(__name__)
    
    @app.route('/')
    def debug_info():
        return f"Deployment Debug - Error: {startup_error}"

# This is the entry point for Vercel
if __name__ == "__main__":
    app.run(debug=False)
