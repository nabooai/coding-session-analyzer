# typed: false
# frozen_string_literal: true

# Homebrew formula for csa-client. This repo is the tap:
#   brew tap csa/csa https://github.com/nabooai/coding-session-analyzer
#   brew install csa-client
#
# After each client release, set `version` to the tag without its `v` and each sha256 to the
# matching line of that release's checksums.txt (the release job summary prints both). The two
# fields are marked RELEASE-AUTOMATION.
class CsaClient < Formula
  desc "Watches Claude Code sessions and ships them to your team's CSA server"
  homepage "https://github.com/nabooai/coding-session-analyzer"
  version "0.1.32" # RELEASE-AUTOMATION: set to the git tag (no leading `v`)

  on_macos do
    on_arm do
      url "https://github.com/nabooai/coding-session-analyzer/releases/download/v#{version}/csa-client-darwin-arm64"
      sha256 "08f2993718f402f25dd0adb522cf89206c72c710037443ed3077f9a2cd775061" # RELEASE-AUTOMATION
    end
    on_intel do
      url "https://github.com/nabooai/coding-session-analyzer/releases/download/v#{version}/csa-client-darwin-amd64"
      sha256 "a1ff284f2e1cdd98f1defb6f681e3af82294bb7911330505ab30a9b594a6b536" # RELEASE-AUTOMATION
    end
  end

  on_linux do
    on_arm do
      url "https://github.com/nabooai/coding-session-analyzer/releases/download/v#{version}/csa-client-linux-arm64"
      sha256 "618c5339cb1e2f334d2b8e3989397eee65b066460776a8309937551393a909d8" # RELEASE-AUTOMATION
    end
    on_intel do
      url "https://github.com/nabooai/coding-session-analyzer/releases/download/v#{version}/csa-client-linux-amd64"
      sha256 "77fabd5fdbf9ae7c9ff20e6a6fb90dc617c8dd3adc98dbf269b858dab9677f14" # RELEASE-AUTOMATION
    end
  end

  def install
    # The download lands under its asset name (csa-client-<os>-<arch>); install it as `csa-client`.
    bin.install Dir["csa-client-*"].first => "csa-client"
  end

  test do
    # Release automation stamps the same tag into the binary (-ldflags "-X main.version=<tag>"),
    # so the reported version contains the formula version.
    assert_match version.to_s, shell_output("#{bin}/csa-client version")
  end
end
