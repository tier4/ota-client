import ota_client_stub
import ota_client_service

if __name__ == "__main__":
    ota_client_stub = OtaClientStub()
    ota_client_service = OtaClientService(ota_client_stub)
