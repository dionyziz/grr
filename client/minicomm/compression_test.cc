#include "experimental/users/bgalehouse/grr_cpp_client/compression.h"

#include "testing/base/public/gunit.h"

namespace grr {

TEST(CompressionTest, RoundTrip) {
  static const string kSentance =
      "The quick sly fox jumped over the lazy dogs.";
  EXPECT_EQ(ZLib::Inflate(ZLib::Deflate(kSentance)), kSentance);

  static const string kZeros(2048, '\0');
  EXPECT_EQ(ZLib::Inflate(ZLib::Deflate(kZeros)), kZeros);

  static const string kShort = "A";
  EXPECT_EQ(ZLib::Inflate(ZLib::Deflate(kShort)), kShort);
}
}  // namespace grr
