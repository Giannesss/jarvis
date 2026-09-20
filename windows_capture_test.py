import asyncio
from winrt.windows.media.capture import MediaCapture

async def main():
    print("CREATING CAPTURE...")

    capture = MediaCapture()

    print("INITIALIZING...")
    await capture.initialize_async()

    print("MEDIA CAPTURE INITIALIZED: OK")

asyncio.run(main())