"""The main covimerage module."""
import copy
import itertools
import os
import re

import attr
from click.utils import string_types

from .coveragepy import CoverageData
from .logger import logger
from .utils import (
    find_executable_files, get_fname_and_fobj_and_str, is_executable_line,
)

DEFAULT_COVERAGE_DATA_FILE = '.coverage_covimerage'
RE_FUNC_PREFIX = re.compile(
    r'^\s*fu(?:n(?:(?:c(?:t(?:i(?:o(?:n)?)?)?)?)?)?)?!?\s+')
RE_CONTINUING_LINE = re.compile(r'\s*\\')
RE_SOURCED_TIMES = re.compile(r'Sourced (\d+) time')
RE_SNR_PREFIX = re.compile(r'^<SNR>\d+_')


def get_version():
    from covimerage.__version__ import version
    return version


@attr.s
class Line(object):
    """A source code line."""
    line = attr.ib()
    count = attr.ib(default=None)
    total_time = attr.ib(default=None)
    self_time = attr.ib(default=None)
    _parsed_line = None


@attr.s(hash=True)
class Script(object):
    path = attr.ib()
    lines = attr.ib(default=attr.Factory(dict), repr=False, hash=False)
    # List of line numbers for dict functions.
    dict_functions = attr.ib(default=attr.Factory(set), repr=False, hash=False)
    # List of line numbers for dict functions that have been mapped already.
    mapped_dict_functions = attr.ib(default=attr.Factory(set), repr=False,
                                    hash=False)
    func_to_lnums = attr.ib(default=attr.Factory(dict), repr=False, hash=False)
    sourced_count = attr.ib(default=None)

    def parse_function(self, lnum, line):
        m = re.match(RE_FUNC_PREFIX, line)
        if m:
            f = line[m.end():].split('(', 1)[0]
            if '.' in f:
                self.dict_functions.add(lnum)

            if f.startswith('<SID>'):
                f = 's:' + f[5:]
            elif f.startswith('g:'):
                f = f[2:]
            self.func_to_lnums.setdefault(f, []).append(lnum)


@attr.s
class Function(object):
    name = attr.ib()
    total_time = attr.ib(default=None)
    self_time = attr.ib(default=None)
    lines = attr.ib(default=attr.Factory(dict), repr=False)
    source = None


@attr.s
class MergedProfiles(object):
    profiles = attr.ib(default=attr.Factory(list))
    source = attr.ib(default=attr.Factory(list))
    append_to = attr.ib(default=None)

    _coveragepy_data = None

    def __setattr__(self, name, value):
        """Invalidate cache if profiles get changed."""
        if name == 'profiles':
            self._coveragepy_data = None
        super(MergedProfiles, self).__setattr__(name, value)

    def add_profile_files(self, *profile_files):
        for f in profile_files:
            p = Profile(f)
            p.parse()
            self.profiles.append(p)

    @property
    def scripts(self):
        return itertools.chain.from_iterable(p.scripts for p in self.profiles)

    @property
    def scriptfiles(self):
        return {s.path for s in self.scripts}

    @property
    def lines(self):
        def merge_lines(line1, line2):
            assert line1.line == line2.line
            new = Line(line1.line)
            if line1.count is None:
                new.count = line2.count
            elif line2.count is None:
                new.count = line1.count
            else:
                new.count = line1.count + line2.count
            return new

        lines = {}
        for p in self.profiles:
            for s, s_lines in p.lines.items():
                if s.path in lines:
                    new = lines[s.path]
                    for lnum, line in s_lines.items():
                        if lnum in new:
                            new[lnum] = merge_lines(new[lnum], line)
                        else:
                            new[lnum] = copy.copy(line)
                else:
                    lines[s.path] = copy.copy(s_lines)

                if s.sourced_count and s_lines:
                    # Fix line count for first line.
                    # https://github.com/vim/vim/issues/2103.
                    line1 = lines[s.path][1]
                    if not line1.count and is_executable_line(line1.line):
                        line1.count = 1
        return lines

    def _get_coveragepy_data(self):
        if self.append_to and os.path.exists(self.append_to):
            data = CoverageData(data_file=self.append_to)
        else:
            data = CoverageData()

        cov_dict = {}
        cov_file_tracers = {}

        source_files = []
        for source in self.source:
            source = os.path.abspath(source)
            if os.path.isfile(source):
                source_files.append(source)
            else:
                source_files.extend(find_executable_files(source))
        logger.debug('source_files: %r', source_files)

        for fname, lines in self.lines.items():
            fname = os.path.abspath(fname)
            if self.source and fname not in source_files:
                logger.info('Ignoring non-source: %s', fname)
                continue

            cov_dict[fname] = {
                # lnum: line.count for lnum, line in lines.items()
                # XXX: coveragepy does not support hit counts?!
                lnum: None for lnum, line in lines.items() if line.count
            }
            # Add our plugin as file tracer, so that it gets used with e.g.
            # `coverage annotate`.
            cov_file_tracers[fname] = 'covimerage.CoveragePlugin'
        measured_files = cov_dict.keys()
        non_measured_files = set(source_files) - set(measured_files)
        for fname in non_measured_files:
            logger.debug('Non-measured file: %s', fname)
            cov_dict[fname] = {}
            cov_file_tracers[fname] = 'covimerage.CoveragePlugin'

        data.add_lines(cov_dict)
        data.cov_data.add_file_tracers(cov_file_tracers)
        return data.cov_data

    def get_coveragepy_data(self):
        if self._coveragepy_data is not None:
            return self._coveragepy_data
        else:
            self._coveragepy_data = self._get_coveragepy_data()
        return self._coveragepy_data

    # TODO: move to CoverageWrapper
    def write_coveragepy_data(self, data_file='.coverage'):
        import coverage

        cov_data = self.get_coveragepy_data()
        try:
            line_counts = cov_data.line_counts()
        except AttributeError:
            line_counts = coverage.data.line_counts(cov_data)
        if not line_counts:
            logger.warning('Not writing coverage file: no data to report!')
            return False

        if isinstance(data_file, string_types):
            logger.info('Writing coverage file %s.', data_file)
            try:
                write_file = cov_data.write_file
            except AttributeError:
                # coveragepy 5
                write_file = cov_data._write_file
            write_file(data_file)
        else:
            try:
                filename = data_file.name
            except AttributeError:
                filename = str(data_file)
            logger.info('Writing coverage file %s.', filename)
            cov_data.write_fileobj(data_file)
        return True


@attr.s
class Profile(object):
    fname = attr.ib()
    # TODO: make this a dict?  (scripts_by_fname)
    scripts = attr.ib(default=attr.Factory(list))
    anonymous_functions = attr.ib(default=attr.Factory(dict))
    _fobj = None
    _fstr = None

    def __attrs_post_init__(self):
        self.fname, self._fobj, self._fstr = get_fname_and_fobj_and_str(
            self.fname)
        self.scripts_by_fname = {}

    @property
    def scriptfiles(self):
        return {s.path for s in self.scripts}

    @property
    def lines(self):
        return {s: s.lines for s in self.scripts}

    def _get_anon_func_script_line(self, func):
        len_func_lines = len(func.lines)
        found = []
        for s in self.scripts:
            for lnum in s.dict_functions:
                script_lnum = lnum + 1
                len_script_lines = len(s.lines)

                func_lnum = 0
                script_is_func = True
                script_line = s.lines[script_lnum].line
                while (script_lnum <= len_script_lines and
                       func_lnum < len_func_lines):
                    script_lnum += 1
                    next_line = s.lines[script_lnum].line
                    m = RE_CONTINUING_LINE.match(next_line)
                    if m:
                        script_line += next_line[m.end():]
                        continue
                    func_lnum += 1
                    if script_line != func.lines[func_lnum].line:
                        script_is_func = False
                        break
                    script_line = s.lines[script_lnum].line

                if script_is_func:
                    found.append((s, lnum))

        if found:
            if len(found) > 1:
                logger.warning(
                    'Found multiple sources for anonymous function %s (%s).',
                    func.name, (', '.join('%s:%d' % (f[0].path, f[1])
                                          for f in found)))

            for s, lnum in found:
                if lnum in s.mapped_dict_functions:
                    # More likely to happen with merged profiles.
                    logger.debug(
                        'Found already mapped dict function again (%s:%d).',
                        s.path, lnum)
                    continue
                s.mapped_dict_functions.add(lnum)
                return (s, lnum)
            return found[0]

    def get_anon_func_script_line(self, func):
        funcname = func.name
        try:
            return self.anonymous_functions[funcname]
        except KeyError:
            f_info = self._get_anon_func_script_line(func)
            if f_info is not None:
                (script, lnum) = f_info
                self.anonymous_functions[func.name] = (script, lnum)
                return self.anonymous_functions[funcname]

    def find_func_in_source(self, func):
        funcname = func.name
        if funcname.isdigit():
            # This is an anonymous function, which we need to lookup based on
            # its source contents.
            return self.get_anon_func_script_line(func)

        m = RE_SNR_PREFIX.match(funcname)
        if m:
            funcname = 's:' + funcname[m.end():]

        found = []
        for script in self.scripts:
            try:
                lnums = script.func_to_lnums[funcname]
            except KeyError:
                continue

            for script_lnum in lnums:
                if self.source_contains_func(script, script_lnum, func):
                    found.append((script, script_lnum))
        if found:
            if len(found) > 1:
                logger.warning('Found multiple sources for function %s (%s).',
                               func, (', '.join('%s:%d' % (f[0].path, f[1])
                                                for f in found)))
            return found[0]
        return None

    @staticmethod
    def source_contains_func(script, script_lnum, func):
        for [f_lnum, f_line] in func.lines.items():
            s_line = script.lines[script_lnum + f_lnum]

            # XXX: might not be the same, since function lines
            # are joined, while script lines might be spread
            # across several lines (prefixed with \).
            script_source = s_line.line
            if script_source != f_line.line:
                while True:
                    peek = script.lines[script_lnum + f_lnum + 1]
                    m = RE_CONTINUING_LINE.match(peek.line)
                    if m:
                        script_source += peek.line[m.end():]
                        script_lnum += 1
                        continue
                    if script_source == f_line.line:
                        break
                    return False
        return True

    def parse(self):
        logger.debug('Parsing file: %s', self._fstr)
        if self._fobj:
            return self._parse(self._fobj)
        with open(self.fname, 'r') as file_object:
            return self._parse(file_object)

    def _parse(self, file_object):
        in_script = None
        in_function = None
        plnum = lnum = 0

        def skip_to_count_header():
            skipped = 0
            while True:
                next_line = next(file_object)
                skipped += 1
                if next_line.startswith('count'):
                    break
            return skipped

        functions = []
        for line in file_object:
            plnum += 1
            line = line.rstrip('\r\n')
            if line == '':
                if in_function:
                    functions += [in_function]
                in_script = None
                in_function = None
                continue

            if in_script or in_function:
                lnum += 1
                try:
                    count, total_time, self_time = parse_count_and_times(line)
                except Exception as exc:
                    logger.warning(
                        'Could not parse count/times (%s:%d, %r): %r.',
                        self._fstr, plnum, line, exc)
                    continue
                source_line = line[28:]

                if in_script:
                    if count is None and RE_CONTINUING_LINE.match(source_line):
                        count = in_script.lines[lnum-1].count
                    in_script.lines[lnum] = Line(
                        line=source_line, count=count,
                        total_time=total_time, self_time=self_time)
                    if count or lnum == 1:
                        # Parse line 1 always, as a workaround for
                        # https://github.com/vim/vim/issues/2103.
                        in_script.parse_function(lnum, source_line)
                else:
                    if count is None:
                        if is_executable_line(source_line):
                            # Functions do not have continued lines, assume 0.
                            count = 0
                    line = Line(line=source_line, count=count,
                                total_time=total_time, self_time=self_time)
                    in_function.lines[lnum] = line

            elif line.startswith('SCRIPT  '):
                fname = line[8:]
                in_script = Script(fname)
                logger.debug('Parsing script %s', in_script)
                self.scripts.append(in_script)
                self.scripts_by_fname[fname] = in_script

                next_line = next(file_object)
                m = RE_SOURCED_TIMES.match(next_line)
                in_script.sourced_count = int(m.group(1))

                plnum += skip_to_count_header() + 1
                lnum = 0

            elif line.startswith('FUNCTION  '):
                func_name = line[10:-2]
                in_function = Function(name=func_name)
                logger.debug('Parsing function %s', in_function)
                while True:
                    next_line = next(file_object)
                    if not next_line:
                        break
                    plnum += 1
                    if next_line.startswith('count'):
                        break
                    if next_line.startswith('    Defined:'):
                        defined = next_line[13:]
                        fname, _, lnum = defined.rpartition(":")
                        if not fname:
                            fname, _, lnum = defined.rpartition(' line ')
                        fname = os.path.expanduser(fname)
                        in_function.source = (self.scripts_by_fname[fname],
                                              int(lnum))
                lnum = 0
        self.map_functions(functions)

    def map_functions(self, functions):
        while functions:
            prev_count = len(functions)
            for f in functions:
                if self.map_function(f):
                    functions.remove(f)
            new_count = len(functions)
            if prev_count == new_count:
                break

        for f in functions:
            logger.error('Could not find source for function: %s', f.name)

    def map_function(self, f):
        if f.source:
            script, script_lnum = f.source
        else:
            script_line = self.find_func_in_source(f)
            if not script_line:
                return False
            script, script_lnum = script_line

        # Assign counts from function to script.
        for [f_lnum, f_line] in f.lines.items():
            s_lnum = script_lnum + f_lnum
            try:
                s_line = script.lines[s_lnum]
            except KeyError:
                logger.warning(
                    "Could not find script line for function %s (%d, %d)",
                    f.name, script_lnum, f_lnum,
                )
                return False

            # XXX: might not be the same, since function lines
            # are joined, while script lines might be spread
            # across several lines (prefixed with \).
            script_source = s_line.line
            if script_source != f_line.line:
                while True:
                    try:
                        peek = script.lines[script_lnum + f_lnum + 1]
                    except KeyError:
                        pass
                    else:
                        m = RE_CONTINUING_LINE.match(peek.line)
                        if m:
                            script_source += peek.line[m.end():]
                            script_lnum += 1
                            continue
                    if script_source == f_line.line:
                        break

                    logger.warning(
                        "Script line does not match function line, "
                        "ignoring: %r != %r.",
                        script_source,
                        f_line.line,
                    )
                    return False

            if f_line.count is not None:
                script.parse_function(script_lnum + f_lnum,
                                      f_line.line)
                if s_line.count:
                    s_line.count += f_line.count
                else:
                    s_line.count = f_line.count

                # Assign count to continued lines.
                i = s_lnum + 1
                n = len(script.lines)
                while i < n and RE_CONTINUING_LINE.match(script.lines[i].line):
                    script.lines[i].count = s_line.count
                    i += 1

            if f_line.self_time:
                if s_line.self_time:
                    s_line.self_time += f_line.self_time
                else:
                    s_line.self_time = f_line.self_time
            if f_line.total_time:
                if s_line.total_time:
                    s_line.total_time += f_line.total_time
                else:
                    s_line.total_time = f_line.total_time
        return True


def parse_count_and_times(line):
    count = line[0:5]
    if count == '':
        return 0, None, None
    if count == '     ':
        count = None
    else:
        count = int(count)
    total_time = line[8:16]
    if total_time == '        ':
        total_time = None
    else:
        total_time = float(total_time)
    self_time = line[19:27]
    if self_time == '        ':
        self_time = None
    else:
        self_time = float(self_time)

    return count, total_time, self_time


def coverage_init(reg, options):
    """
    Called from coverage.py when used as plugin in .coveragerc.

    This is not really necessary, but let's keep it so that e.g.
    `coverage annotate` can be used.

    [run]
    plugins = covimerage
    """
    from .coveragepy import CoveragePlugin

    reg.add_file_tracer(CoveragePlugin())
