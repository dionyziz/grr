#ifndef EXPERIMENTAL_USERS_BGALEHOUSE_GRR_CPP_CLIENT_UTIL_H_
#define EXPERIMENTAL_USERS_BGALEHOUSE_GRR_CPP_CLIENT_UTIL_H_

#include <string>

namespace grr {
string BytesToHex(const std::string& input);
string UrlDirname(const std::string& input);
}

#endif  // EXPERIMENTAL_USERS_BGALEHOUSE_GRR_CPP_CLIENT_UTIL_H_
