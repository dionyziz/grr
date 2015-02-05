#ifndef EXPERIMENTAL_USERS_BGALEHOUSE_GRR_CPP_CLIENT_HTTP_CONNECTION_H_
#define EXPERIMENTAL_USERS_BGALEHOUSE_GRR_CPP_CLIENT_HTTP_CONNECTION_H_

#include <chrono>
#include <memory>

#include "experimental/users/bgalehouse/grr_cpp_client/comms_utils.h"
#include "experimental/users/bgalehouse/grr_cpp_client/config.h"
#include "experimental/users/bgalehouse/grr_cpp_client/message_queue.h"

namespace grr {
class HttpConnectionManager {
 public:
  // Performs necessary static initialization. Must be called before
  // any threads are spawned.
  static void StaticInit();

  // Creates an http connection manager which uses the information in config to
  // connect to a server. Received messages are added to inbox. Messages are
  // removed from outbox and passed to the server.
  HttpConnectionManager(ClientConfig* config, MessageQueue* inbox,
                        MessageQueue* outbox);
  ~HttpConnectionManager();

  // Enters an event loop to read and write messages to the server. Does not
  // normally return.
  void Run();

 private:
  class Connection;

  friend class Connection;

  Connection* TryEstablishConnection();
  std::chrono::system_clock::time_point last_enrollment_;

  std::unique_ptr<Connection> current_connection_;

  ClientConfig* config_;
  MessageQueue* inbox_;
  MessageQueue* outbox_;
};
}  // namespace grr

#endif  // EXPERIMENTAL_USERS_BGALEHOUSE_GRR_CPP_CLIENT_HTTP_CONNECTION_H_
