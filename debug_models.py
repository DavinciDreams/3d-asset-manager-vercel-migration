#!/usr/bin/env python3
"""
Comprehensive test to debug model display issues
"""
import os
import sys

# Add project root to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault('FLASK_ENV', 'production')

def debug_model_display_issues():
    print("🔍 Debugging Model Display Issues...")
    
    try:
        from app import create_app
        from app.models import Model3D, User
        from bson.objectid import ObjectId
        
        app = create_app()
        
        with app.app_context():
            db = app.config['MONGODB_DB']
            
            print("✅ Step 1: Check raw database content...")
            
            # Check all models in database
            all_models = list(db.models.find({}))
            print(f"   📊 Total models in database: {len(all_models)}")
            
            for i, model in enumerate(all_models):
                print(f"\n   Model {i+1}:")
                print(f"      ID: {model['_id']}")
                print(f"      Name: {model['name']}")
                print(f"      Public: {model['is_public']} (Type: {type(model['is_public'])})")
                print(f"      User ID: {model['user_id']}")
                print(f"      File Format: {model['file_format']}")
                print(f"      Upload Date: {model.get('upload_date')}")
            
            print("\n✅ Step 2: Test Model3D.get_public_models()...")
            
            try:
                public_models, public_total = Model3D.get_public_models(page=1, per_page=10)
                print(f"   📊 get_public_models() returned: {public_total} models")
                
                for i, model in enumerate(public_models):
                    print(f"   Public Model {i+1}: {model.name} (ID: {model.id})")
                    
            except Exception as e:
                print(f"   ❌ get_public_models() failed: {e}")
                import traceback
                traceback.print_exc()
            
            print("\n✅ Step 3: Test Model3D.get_by_id()...")
            
            if all_models:
                test_model_id = str(all_models[0]['_id'])
                print(f"   Testing with ID: {test_model_id}")
                
                try:
                    model = Model3D.get_by_id(test_model_id)
                    if model:
                        print(f"   ✅ get_by_id() success: {model.name}")
                        print(f"      Has required methods: file_extension={hasattr(model, 'file_extension')}, get_file_size_formatted={hasattr(model, 'get_file_size_formatted')}")
                    else:
                        print(f"   ❌ get_by_id() returned None")
                        
                except Exception as e:
                    print(f"   ❌ get_by_id() failed: {e}")
                    import traceback
                    traceback.print_exc()
            
            print("\n✅ Step 4: Test query with exact MongoDB query...")
            
            # Test direct MongoDB query for public models
            public_query = {'is_public': True}
            public_count = db.models.count_documents(public_query)
            public_docs = list(db.models.find(public_query))
            
            print(f"   📊 Direct MongoDB query for public models: {public_count}")
            print(f"   📊 Direct query returned {len(public_docs)} documents")
            
            # Check data types
            for doc in public_docs[:1]:
                print(f"   Data type check for is_public: {type(doc['is_public'])} = {doc['is_public']}")
            
            print("\n✅ Step 5: Test stats calculation...")
            
            try:
                stats = Model3D.get_stats()
                print(f"   📊 Stats: {stats}")
            except Exception as e:
                print(f"   ❌ get_stats() failed: {e}")
            
            print("\n🎯 Diagnosis Summary:")
            
            if len(all_models) == 0:
                print("   ❌ No models in database")
            elif public_count == 0:
                print("   ⚠️  Models exist but none are marked as public")
                print("   💡 Check if is_public field is True (boolean) not 'true' (string)")
            elif public_total == 0:
                print("   ⚠️  Public models exist but get_public_models() returns 0")
                print("   💡 Check Model3D class query logic")
            else:
                print(f"   ✅ {public_total} public models should be displaying")
                
    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_model_display_issues()
