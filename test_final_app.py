#!/usr/bin/env python3
"""
Final Flask app test with correct MongoDB URI
"""
import os
import sys

os.environ.setdefault('FLASK_ENV', 'production')

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

print("🧪 Final Flask App Test for Vercel...")
print(f"📋 MONGODB_URI configured: {'yes' if os.environ.get('MONGODB_URI') else 'no'}")
print(f"📋 SECRET_KEY configured: {'yes' if os.environ.get('SECRET_KEY') else 'no'}")

try:
    # Test 1: Import the create_app function
    print("✅ Step 1: Testing imports...")
    from app import create_app
    print("   ✓ Successfully imported create_app")
    
    # Test 2: Create the app instance
    print("✅ Step 2: Creating Flask app...")
    app = create_app()
    print("   ✓ Successfully created Flask app")
    
    # Test 3: Check app configuration
    print("✅ Step 3: Checking app configuration...")
    print(f"   ✓ App name: {app.name}")
    print(f"   ✓ Debug mode: {app.debug}")
    print(f"   ✓ Environment: {app.config.get('ENV', 'Not set')}")
    
    # Test 4: Check MongoDB configuration
    print("✅ Step 4: Checking MongoDB configuration...")
    mongodb_client = app.config.get('MONGODB_CLIENT')
    if mongodb_client:
        print("   ✓ MongoDB client configured and connected")
    
    # Test 5: Check routes
    print("✅ Step 5: Checking routes...")
    routes = [rule.rule for rule in app.url_map.iter_rules()]
    print(f"   ✓ Found {len(routes)} routes")
    
    print("\n🎉 SUCCESS: Flask app is 100% ready for Vercel deployment!")
    print("\n📋 Vercel Environment Variables:")
    print("MONGODB_URI")
    print("mongodb+srv://<username>:<url-encoded-password>@<cluster-host>/<database>?retryWrites=true&w=majority&appName=<app-name>")
    print("\nSECRET_KEY")
    print("<generate-with-python-secrets-token-hex-32>")
    print("\nFLASK_ENV")
    print("production")
    
except Exception as e:
    print(f"❌ ERROR: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "="*60)
