mkdir %USERPROFILE%\build\
cd %USERPROFILE%\build\ || echo "Cant switch to build directory" && exit /b 1

:: Install protobuf compiler - needed for compiling protobufs
powershell -NoProfile -ExecutionPolicy unrestricted -Command "(new-object System.Net.WebClient).DownloadFile('https://github.com/google/protobuf/releases/download/v2.6.1/protoc-2.6.1-win32.zip', 'protoc-2.6.1-win32.zip')"
7z x protoc-2.6.1-win32.zip
