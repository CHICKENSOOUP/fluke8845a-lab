import csv
from pathlib import Path

import pytest

from fluke8845a.curve_export import write_curve_csv

FIELDS = ['sample_index', 'timestamp_local', 'elapsed_s', 'value_si', 'unit_si', 'raw_reading', 'status', 'error']

def source_csv(path, rows):
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))

def test_preserves_rows_and_restarts_average_at_invalid_and_missing_sample(tmp_path):
    source, output = tmp_path / 'measurements.csv', tmp_path / 'curve.csv'
    source_csv(source, [
        dict(sample_index='1', elapsed_s='0', value_si='1', raw_reading='1.000000', status='ok', error=''),
        dict(sample_index='2', elapsed_s='1', value_si='3', raw_reading='3.000000', status='ok', error=''),
        dict(sample_index='3', elapsed_s='2', value_si='', raw_reading='=malicious', status='timeout', error='@error'),
        dict(sample_index='4', elapsed_s='3', value_si='9', raw_reading='9', status='ok', error=''),
        dict(sample_index='6', elapsed_s='5', value_si='5', raw_reading='5', status='ok', error=''),
        dict(sample_index='7', elapsed_s='6', value_si='7', raw_reading='7', status='ok', error=''),
    ])
    before = source.read_bytes()
    count = write_curve_csv(source, output, 2)
    rows = read_csv(output)
    assert count == 6
    assert [row['moving_average_value_si'] for row in rows] == ['1.0', '2.0', '', '9.0', '5.0', '6.0']
    assert list(rows[0]) == ['timestamp_local', 'elapsed_s', 'value_si', 'moving_average_value_si', 'unit_si']
    details = read_csv(tmp_path / 'curve_detailed.csv')
    assert details[0]['raw_reading'] == '1.000000'
    assert all(row['moving_average_window_points'] == '2' for row in details)
    assert details[2]['raw_reading'] == '=malicious' and details[2]['error'] == '@error'
    assert source.read_bytes() == before

def test_refuses_source_and_existing_destination_without_modification(tmp_path):
    source, output = tmp_path / 'measurements.csv', tmp_path / 'existing.csv'
    source_csv(source, [])
    output.write_text('do not overwrite')
    original = source.read_bytes()
    with pytest.raises(ValueError):
        write_curve_csv(source, source, 2)
    with pytest.raises(FileExistsError):
        write_curve_csv(source, output, 2)
    assert source.read_bytes() == original
    assert output.read_text() == 'do not overwrite'

def test_incomplete_tail_is_excluded(tmp_path):
    source, output = tmp_path / 'measurements.csv', tmp_path / 'curve.csv'
    source_csv(source, [dict(sample_index='1', elapsed_s='0', value_si='1', raw_reading='1', status='ok', error='')])
    with source.open('a', encoding='utf-8') as f:
        f.write('2,1,2,"unfinished')
    assert write_curve_csv(source, output, 1) == 1
    assert len(read_csv(output)) == 1

@pytest.mark.parametrize('window', [0, -1, 1.5, True])
def test_invalid_window_is_rejected_without_output(tmp_path, window):
    source, output = tmp_path / 'measurements.csv', tmp_path / 'curve.csv'
    source_csv(source, [])
    with pytest.raises(ValueError):
        write_curve_csv(source, output, window)
    assert not output.exists()


def test_signed_and_quoted_raw_fields_are_unchanged(tmp_path):
    source, output = tmp_path / 'measurements.csv', tmp_path / 'curve.csv'
    source_csv(source, [
        dict(sample_index='1', elapsed_s='0', value_si='-1.23e-6', raw_reading='-1.23000E-06', status='ok', error='first\nsecond'),
        dict(sample_index='2', elapsed_s='1', value_si='nan', raw_reading='NaN', status='ok', error=''),
        dict(sample_index='3', elapsed_s='2', value_si='2e-6', raw_reading='2E-6', status='ok', error=''),
    ])
    assert write_curve_csv(source, output, 2) == 3
    rows = read_csv(output)
    assert rows[0]['value_si'] == '-1.23e-6'
    details = read_csv(tmp_path / 'curve_detailed.csv')
    assert details[0]['raw_reading'] == '-1.23000E-06'
    assert details[0]['error'] == 'first\nsecond'
    assert rows[1]['moving_average_value_si'] == ''
    assert rows[1]['value_si'] == ''
    assert details[1]['value_si'] == 'nan'
    assert float(rows[2]['moving_average_value_si']) == pytest.approx(2e-6)


def test_malformed_rows_rejected_without_output(tmp_path):
    source, output = tmp_path / 'measurements.csv', tmp_path / 'curve.csv'
    source.write_text('sample_index,elapsed_s,value_si,status\n1,0,1,ok,extra\n', encoding='utf-8')
    with pytest.raises(ValueError):
        write_curve_csv(source, output, 2)
    assert not output.exists()
    assert list(tmp_path.glob('.curve-export-*.tmp')) == []


def test_simple_export_keeps_time_and_unit_and_detailed_prevents_overwrite(tmp_path):
    source, output = tmp_path / 'measurements.csv', tmp_path / 'curve.csv'
    source_csv(source, [dict(sample_index='1', timestamp_local='2026-10-28T12:34:56+08:00', elapsed_s='0.25', value_si='1.2300e-6', unit_si='A', raw_reading='1.2300E-6', status='ok', error='')])
    detailed = tmp_path / 'curve_detailed.csv'
    detailed.write_text('existing')
    with pytest.raises(FileExistsError):
        write_curve_csv(source, output, 20)
    assert not output.exists()
    assert detailed.read_text() == 'existing'
    detailed.unlink()
    assert write_curve_csv(source, output, 20) == 1
    assert read_csv(output) == [dict(timestamp_local='2026-10-28T12:34:56+08:00', elapsed_s='0.25', value_si='1.2300e-6', moving_average_value_si='1.23e-06', unit_si='A')]
    assert read_csv(detailed)[0]['raw_reading'] == '1.2300E-6'


def test_publish_failure_rolls_back_companion(tmp_path, monkeypatch):
    from fluke8845a import curve_export
    source, output = tmp_path / 'measurements.csv', tmp_path / 'curve.csv'
    source_csv(source, [])
    publish = curve_export._publish_without_overwrite
    def fail_main(staged, destination):
        if destination == output:
            raise OSError('simulated failure')
        publish(staged, destination)
    monkeypatch.setattr(curve_export, '_publish_without_overwrite', fail_main)
    with pytest.raises(OSError, match='simulated failure'):
        write_curve_csv(source, output, 2)
    assert not output.exists()
    assert not (tmp_path / 'curve_detailed.csv').exists()
    assert list(tmp_path.glob('.curve-export-*.tmp')) == []


def test_companion_cannot_overwrite_source(tmp_path):
    source = tmp_path / 'curve_detailed.csv'
    output = tmp_path / 'curve.csv'
    source_csv(source, [])
    before = source.read_bytes()
    with pytest.raises(ValueError):
        write_curve_csv(source, output, 2)
    assert source.read_bytes() == before
    assert not output.exists()
