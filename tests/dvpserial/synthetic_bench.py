"""Fresh eight-lane digital bench catalog, authored without a customer input.

This generator accepts no project/profile input. Its arbitrary register layout
exists only in the independent wire double. It is not a deployment template.
"""

LANES = 8
BANKS = 4


def catalog():
    commands = {}
    reads = {'M1000', 'M1002', 'M451'}
    def vector(base, count):
        return [f'M{base+i}' for i in range(count)]
    def command(name, on, off, ack, edge, count, selector, calibration=False):
        guards = [['M451', f'M{off+i}'] for i in range(count)]
        if selector == 'lane':
            for i, row in enumerate(guards):
                row.append(f'M{336+i//2}')
                if calibration:
                    row.extend(['M449', f'M{400+i}'])
        for suffix, coils, expected, stop, edges in (
            ('start', vector(on,count), 1, guards, vector(edge,count)),
            ('stop', vector(off,count), 0, [[] for _ in range(count)], [])):
            spec = dict(selector=selector, coils=coils, ack_devices=vector(ack,count),
                        ack_expected=expected, stop_guards=stop,
                        release_edge_devices=edges, is_start=suffix=='start',
                        effect='synthetic digital request')
            commands[f'bench.{name}.{suffix}'] = spec
            commands[f'bench.all_{name}.{suffix}'] = dict(spec, selector=None)
            reads.update(coils+spec['ack_devices']+edges)
            for row in stop: reads.update(row)
    command('lane',256,272,288,304,LANES,'lane')
    command('bank',320,336,352,368,BANKS,'bank')
    command('calibration',384,400,704,432,LANES,'lane',True)
    for suffix, coil, expected in [('start',448,1),('stop',449,0)]:
        commands[f'bench.all_calibration.{suffix}'] = dict(
            selector=None, coils=[f'M{coil}'], ack_devices=vector(704,LANES),
            ack_expected=expected, stop_guards=[['M451','M449']+vector(400,LANES)] if expected else [[]],
            release_edge_devices=['M450'] if expected else [], is_start=bool(expected),
            effect='synthetic simultaneous calibration')
        reads.update([f'M{coil}','M450'])
    reads.update(f'D{2300+10*i}' for i in range(LANES))
    reads.add('D2400')
    observations = {}
    for name, kind, base, count, scale, signed, unit in [
        ('display_sample','D',256,LANES,0.1,True,'V'),
        ('raw_sample','D',600,LANES,0.01,True,'V'),
        ('duty','D',640,LANES,0.1,False,'%'),
        ('lane_request','M',288,LANES,1,False,'bool'),
        ('lane_permit','M',480,LANES,1,False,'bool'),
        ('bank_request','M',352,BANKS,1,False,'bool'),
        ('bank_permit','M',656,BANKS,1,False,'bool'),
        ('bank_trip','M',640,BANKS,1,False,'bool'),
        ('bank_output_bits','Y',0,BANKS,1,False,'PLC_OUTPUT_BIT'),
        ('lane_output_bits','Y',16,LANES,1,False,'PLC_OUTPUT_BIT'),
        ('calibration_active','M',704,LANES,1,False,'bool'),
        ('gain_a','D',2600,LANES,1,False,'TEST_RAW'),
        ('gain_b','D',2620,LANES,1,False,'TEST_RAW'),
        ('gain_c','D',2640,LANES,1,False,'TEST_RAW')]:
        def device(n): return f'Y{n:o}' if kind=='Y' else f'{kind}{n}'
        observations[name] = dict(device=device(base),count=count,scale=scale,signed=signed,unit=unit)
        reads.update(device(base+i) for i in range(count))
    # A separate four-bit window exercises octal aliases and a catalog boundary.
    reads.update(f'Y{n:o}' for n in range(24,28))
    for lane in range(LANES):
        state=f'state_{lane+1:02}';reason=f'reason_{lane+1:02}'
        observations[state]=dict(device=f'D{2800+3*lane}',count=1,
            enum={'0':'WAITING','1':'RUNNING','2':'COMPLETE','3':'STOPPED','5':'PRECONDITION_MISSING'},
            reason_field=reason,precondition_value=5)
        observations[f'elapsed_{lane+1:02}']=dict(device=f'D{2801+3*lane}',count=1,unit='ticks')
        observations[reason]=dict(device=f'D{2802+3*lane}',count=1,
            enum={'0':'NONE','1':'PERMISSION_OR_PROTECTION_LOSS','6':'TIME_LIMIT','7':'READ_ERROR','8':'CANCELLED'})
        reads.update(f'D{2800+3*lane+j}' for j in range(3))
    return dict(schema_version=1,contract_id='SYNTHETIC_DIGITAL_BENCH_V1',revision='SYNTHETIC_R1',
        reference_source_sha256='0'*64,reference_payload_sha256='1'*64,
        qualification='SOFTWARE_ONLY_FROM_SCRATCH; NEVER A DEPLOYMENT PROFILE',
        commands=commands,read_devices=sorted(reads),observations=observations,parameters={})
