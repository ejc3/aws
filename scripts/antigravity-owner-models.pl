#!/usr/bin/perl
# macOS includes Perl even when Xcode's python3 shim cannot run. Update only our
# section in Codex's effective file and use a separate global rule for Claude.
use strict;
use warnings;
use Cwd qw(abs_path);
use Fcntl qw(O_RDONLY O_NOFOLLOW O_NONBLOCK :mode);
use File::Basename qw(dirname);
use File::Path qw(make_path);
use File::Temp qw(tempfile);

die "Run as the intended user\n" if $< == 0;
my $home = abs_path($ENV{HOME}) // die "Missing home directory\n";
my $begin = '<!-- BEGIN ejc3-antigravity-models (managed) -->';
my $end = '<!-- END ejc3-antigravity-models (managed) -->';

sub read_owned {
    my ($path) = @_;
    sysopen(my $in, $path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK) or die "$path: $!\n";
    my @stat = stat($in);
    die "Expected a regular user-owned instruction file: $path\n"
        unless @stat && S_ISREG($stat[2]) && $stat[4] == $< && $stat[7] <= 1048576;
    local $/;
    my $data = <$in> // '';
    close($in) or die "$path: $!\n";
    return ($data, @stat);
}

my ($guide) = read_owned("$home/.gemini/config/rules/ejc3-antigravity-models.md");
$guide =~ s/\A---\ntrigger: always_on\n---\n// or die "Invalid Antigravity rule header\n";
my $block = "$begin\n$guide$end\n";

sub update_instructions {
    my ($path) = @_;
    my $dir = dirname($path);
    make_path($dir, {mode => 0700});
    die "Unexpected instruction directory link: $dir\n" unless abs_path($dir) eq $dir;
    my ($old, @stat) = -e $path || -l $path ? read_owned($path) : ('');
    my $new = $old;
    if (index($old, $begin) >= 0 || index($old, $end) >= 0) {
        my $starts = () = $old =~ /\Q$begin\E/g;
        my $ends = () = $old =~ /\Q$end\E/g;
        die "Incomplete or duplicate managed section in $path\n" unless $starts == 1 && $ends == 1
            && $new =~ s/^\Q$begin\E\n.*?^\Q$end\E(?:\n|\z)/$block/ms;
    } else {
        $new .= "\n\n" if length($old);
        $new .= $block;
    }
    return if $new eq $old;
    my ($out, $tmp) = tempfile('.ejc3-models.XXXXXXXX', DIR => $dir, UNLINK => 1);
    print {$out} $new or die "$tmp: $!\n";
    close($out) or die "$tmp: $!\n";
    if (@stat) {
        chown(-1, $stat[5], $tmp) or die "$tmp: $!\n" if (stat($tmp))[5] != $stat[5];
        chmod($stat[2] & 0777, $tmp) or die "$tmp: $!\n";
        my ($latest, @latest) = read_owned($path);
        die "Concurrent edit to $path; retry later\n" unless $latest eq $old && $latest[1] == $stat[1];
        rename($tmp, $path) or die "$path: $!\n";
    } else {
        link($tmp, $path) or die "$path: $!\n";
    }
}

my $claude = "$home/.claude/CLAUDE.md";
sub codex_target {
    my ($path) = @_;
    return $path unless -l $path;
    # The fleet starter uses this shared file. Other links can point at private data.
    die "Unexpected Codex instruction link: $path\n" unless (abs_path($path) // '') eq $claude;
    return $claude;
}

my $override = "$home/.codex/AGENTS.override.md";
my $codex;
if (-e $override || -l $override) {
    my $target = codex_target($override);
    my ($instructions) = read_owned($target);
    $codex = $target if $instructions =~ /\S/;
}
$codex //= codex_target("$home/.codex/AGENTS.md");
update_instructions($codex);
update_instructions("$home/.claude/rules/ejc3-antigravity-models.md")
    unless -e $claude && (abs_path($claude) // '') eq $codex;
