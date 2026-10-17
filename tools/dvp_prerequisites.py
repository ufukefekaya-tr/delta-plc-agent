"""Read-only selected-route inventory; never opens a serial port or an IDE."""
import argparse
import importlib.metadata
import json
from pathlib import Path
import sys


def inspect(route, port, require_vendor, inventory, ports):
    tools = inventory.get('tools', {}).get('wplsoft', [])
    selected = [p for p in ports if p['port'] == port] if port else []
    issues = []
    if require_vendor and not tools:
        issues.append('WPLSOFT_MISSING')
    if not port:
        issues.append('PORT_SELECTION_REQUIRED')
    elif not selected:
        issues.append('PORT_NOT_ENUMERATED: hardware disconnected or driver unavailable; inspect OS PnP')
    elif route == 'directlink_cdc' and any((p['vid'], p['pid']) != (0x05DD, 0x0100) for p in selected):
        issues.append('CDC_GATEWAY_MISMATCH')
    return dict(status='PREREQUISITES_FOUND' if not issues else 'INCOMPLETE',
                route=route, selected_port=port, ports=ports, wplsoft=tools, issues=issues,
                port_opened=False, plc_contacted=False, live_verified=False,
                note='Port enumeration is not RS232/RS485 detection or target identity verification.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--route', choices=('serial_ascii', 'directlink_cdc'), required=True)
    parser.add_argument('--port')
    parser.add_argument('--require-vendor', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from universal_plc.environment import probe
    from serial.tools.list_ports import comports
    ports = [dict(port=p.device, vid=p.vid, pid=p.pid, hwid=p.hwid, description=p.description)
             for p in comports()]
    result = inspect(args.route, args.port, args.require_vendor, probe(), ports)
    result['python'] = sys.version.split()[0]
    result['packages'] = {p: importlib.metadata.version(p) for p in ('universal-plc-agents', 'mcp', 'pyserial')}
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        with args.output.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
    print(text)
    return 0 if result['status'] == 'PREREQUISITES_FOUND' else 2


if __name__ == '__main__':
    raise SystemExit(main())
