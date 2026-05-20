import ast
import sys

files = [
    r"c:\Users\sawan\Desktop\new_project\Ayurvedic_Book_Processor\app.py",
    r"c:\Users\sawan\Desktop\new_project\Ayurvedic_Book_Processor\ultimate_book_processor.py",
    r"c:\Users\sawan\Desktop\new_project\Ayurvedic_Book_Processor\image_deck_generator.py",
    r"c:\Users\sawan\Desktop\new_project\Ayurvedic_Book_Processor\image_deck_renderer.py",
    r"c:\Users\sawan\Desktop\new_project\Ayurvedic_Book_Processor\utils.py",
]

for file_path in files:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
        ast.parse(code)
        print(f"{file_path} - OK")
    except SyntaxError as e:
        print(f"SyntaxError in {file_path}: {e}")
    except Exception as e:
        print(f"Error checking {file_path}: {e}")
