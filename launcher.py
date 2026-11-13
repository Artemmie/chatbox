"""PyInstaller entry. A window by default; --no-browser runs only the local server."""
import multiprocessing
import sys


def main():
    if "--no-browser" in sys.argv or "--server" in sys.argv:
        import chatbox
        chatbox.main()
        return
    import app
    app.main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
