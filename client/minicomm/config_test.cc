#include "experimental/users/bgalehouse/grr_cpp_client/config.h"

#include <fstream>

#include "experimental/users/bgalehouse/grr_cpp_client/client_test_base.h"
#include "net/proto2/public/text_format.h"
#include "testing/base/public/googletest.h"
#include "testing/base/public/gunit.h"

namespace grr {

class ConfigTest : public grr::ClientTestBase {
};

TEST_F(ConfigTest, BadConfig) {
  WriteConfigFile("A bad config file::");
  EXPECT_FALSE(config_.ReadConfig());
}

TEST_F(ConfigTest, GoodConfig) {
  WriteValidConfigFile(false, true);
  EXPECT_TRUE(config_.ReadConfig());
}

TEST_F(ConfigTest, NoPrivateKey) {
  WriteValidConfigFile(false, true);
  ASSERT_TRUE(config_.ReadConfig());

  EXPECT_TRUE(config_.key().get() == nullptr);
}

TEST_F(ConfigTest, Writeback) {
  WriteValidConfigFile(false, true);
  ASSERT_TRUE(config_.ReadConfig());
  ClientConfiguration config_proto;

  config_.ResetKey();
  const string client_id = config_.ClientId();
  EXPECT_FALSE(client_id.empty());
  ASSERT_TRUE(proto2::TextFormat::ParseFromString(ReadWritebackFile(),
                                                  &config_proto));
  EXPECT_FALSE(config_proto.client_private_key_pem().empty());
  EXPECT_FALSE(config_proto.has_last_server_cert_serial_number());

  EXPECT_TRUE(config_.CheckUpdateServerSerial(100));
  EXPECT_TRUE(config_.CheckUpdateServerSerial(200));
  EXPECT_FALSE(config_.CheckUpdateServerSerial(150));
  EXPECT_TRUE(config_.CheckUpdateServerSerial(200));

  ASSERT_TRUE(proto2::TextFormat::ParseFromString(ReadWritebackFile(),
                                                  &config_proto));
  EXPECT_EQ(200, config_proto.last_server_cert_serial_number());

  // Verify that a new config will have the same client_id.
  ClientConfig new_config(config_filename_);
  ASSERT_TRUE(new_config.ReadConfig());
  EXPECT_EQ(client_id, new_config.ClientId());
}
}  // namespace grr
