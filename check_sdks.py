import sys

try:
    import a2a
    print("A2A Module Content:")
    print(dir(a2a))
except ImportError:
    print("Could not import a2a")

try:
    import adk
    print("\nADK Module Content:")
    print(dir(adk))
except ImportError:
    print("Could not import adk")
