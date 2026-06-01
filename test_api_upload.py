#!/usr/bin/env python3
"""
Test API upload endpoint locally
"""
import os
import sys
import requests
from io import BytesIO

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('FLASK_ENV', 'production')

def test_api_upload():
    print("🧪 Testing API Upload Endpoint...")
    
    try:
        from app import create_app
        app = create_app()
        
        with app.test_client() as client:
            # First, test if the API endpoint exists
            print("✅ Step 1: Testing API endpoint accessibility...")
            
            # Test without authentication (should fail)
            response = client.post('/api/upload')
            print(f"   Status without auth: {response.status_code}")
            print(f"   Response: {response.get_data(as_text=True)[:100]}...")
            
            # Test if we can access the API route list
            print("✅ Step 2: Testing route registration...")
            routes = []
            for rule in app.url_map.iter_rules():
                if '/api/' in rule.rule:
                    routes.append(f"{rule.rule} [{', '.join(rule.methods)}]")
            
            print(f"   Found {len(routes)} API routes:")
            for route in routes:
                print(f"      {route}")
            
            # Check if /api/upload is registered
            upload_routes = [r for r in routes if '/api/upload' in r]
            if upload_routes:
                print(f"   ✅ Upload route found: {upload_routes[0]}")
            else:
                print("   ❌ Upload route NOT found!")
                
        print("\n🔧 Next Steps:")
        print("1. Verify routes are registered correctly")
        print("2. Check authentication flow")
        print("3. Test with proper file upload")
                
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_api_upload()
